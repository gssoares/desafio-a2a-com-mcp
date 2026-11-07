"""Agente Central de Salas: servidor A2A v1.0 por fora, host MCP por dentro.

A ponte vive em `CentralDeSalas`:
- `_tratar_resultado` transforma o `input_required` do MCP em
  TASK_STATE_INPUT_REQUIRED e guarda o `requestState` ligado a Task;
- `_continuar` recebe a `escolha=...` e repete o `tools/call` original com um id
  novo, levando `inputResponses` e o `requestState` ecoado sem modificacao.

O agente nao decide nada de dominio: conflito, politica e alternativas vem do
servidor MCP. Ele so traduz protocolo, por regra fixa e sem LLM.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import secrets
import socket
import sys
from dataclasses import dataclass
from typing import Any

import uvicorn
from starlette.applications import Starlette

from a2a.helpers import get_message_text, new_text_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.context import ServerCallContext
from a2a.server.routes import DefaultServerCallContextBuilder, create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentProvider,
    AgentSkill,
    Task,
    TaskState,
    TaskStatus,
)

from cliente_mcp import ClienteMCP, ErroMCP

log = logging.getLogger("agente")

HOST = os.environ.get("AGENTE_HOST", "localhost")
PORTA = int(os.environ.get("AGENTE_PORT", "7300"))
URL_PUBLICA = os.environ.get("AGENTE_URL", f"http://127.0.0.1:{PORTA}")
MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp")

TOOL_RESERVA = "reservar_sala"
URI_POLITICA = "politica://uso"

PEDIDO = re.compile(
    r"^\s*reservar\s+sala=(?P<sala>\S+)\s+inicio=(?P<inicio>\S+)\s+fim=(?P<fim>\S+)\s+responsavel=(?P<responsavel>.+?)\s*$"
)
ESCOLHA = re.compile(r"^\s*escolha=(?P<valor>\S+)\s*$")
TRACEPARENT = re.compile(r"^[0-9a-f]{2}-(?P<trace>[0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$")


@dataclass
class Pausa:
    """O que o agente guarda de uma Task pausada. `request_state` e opaco: so ecoado."""

    argumentos: dict[str, Any]
    chave: str
    request_state: str
    alternativas: list[str]
    politica: str
    trace_id: str


def trace_id_do(contexto: RequestContext) -> str | None:
    cabecalhos = contexto.call_context.state.get("headers") or {}
    valor = (cabecalhos.get("traceparent") or "").strip().lower()
    achado = TRACEPARENT.match(valor)
    return achado.group("trace") if achado else None


def alternativas_da(pedido: dict[str, Any]) -> list[str]:
    """Le as opcoes do `requestedSchema` da elicitation, na ordem em que vieram."""
    schema = (pedido.get("params") or {}).get("requestedSchema") or {}
    campo = (schema.get("properties") or {}).get("sala") or {}
    if "enum" in campo:
        return list(campo["enum"])
    return [campo["const"]] if "const" in campo else []


def texto_do(resultado: dict[str, Any]) -> str:
    return " ".join(c.get("text", "") for c in resultado.get("content", []) if c.get("type") == "text")


class CentralDeSalas(AgentExecutor):
    def __init__(self, mcp: ClienteMCP) -> None:
        self.mcp = mcp
        # Estado pausado por Task: task_id -> Pausa. Nunca sai deste processo.
        self.pausas: dict[str, Pausa] = {}

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        tarefa = context.current_task
        if tarefa is None:
            tarefa = Task(
                id=context.task_id,
                context_id=context.context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
                history=[context.message],
            )
            await event_queue.enqueue_event(tarefa)
        updater = TaskUpdater(event_queue, tarefa.id, tarefa.context_id)
        texto = get_message_text(context.message) if context.message else ""

        def responder(msg: str):
            return new_text_message(msg, context_id=tarefa.context_id, task_id=tarefa.id)

        try:
            if tarefa.id in self.pausas:
                await self._continuar(tarefa.id, texto, trace_id_do(context), updater, responder)
            else:
                await self._iniciar(tarefa.id, texto, trace_id_do(context), updater, responder)
        except ErroMCP as e:
            self.pausas.pop(tarefa.id, None)
            await updater.failed(responder(e.mensagem))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        tarefa = context.current_task
        if tarefa is None:
            return
        self.pausas.pop(tarefa.id, None)
        await TaskUpdater(event_queue, tarefa.id, tarefa.context_id).cancel()

    async def _iniciar(self, task_id: str, texto: str, trace_id: str | None, updater: TaskUpdater, responder) -> None:
        await updater.start_work()
        achado = PEDIDO.match(texto)
        if not achado:
            await updater.failed(responder(
                "Pedido invalido: use reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>"
            ))
            return
        trace_id = trace_id or secrets.token_hex(16)
        argumentos = achado.groupdict()

        # Descoberta em runtime: a tool precisa existir no tools/list antes de ser chamada.
        tools = {t["name"] for t in await self.mcp.listar_tools(trace_id)}
        if TOOL_RESERVA not in tools:
            await updater.failed(responder(f"O servidor MCP nao oferece a tool {TOOL_RESERVA}"))
            return
        politica = self._versao(await self.mcp.ler_resource(URI_POLITICA, trace_id))

        resultado = await self.mcp.chamar_tool(TOOL_RESERVA, argumentos, trace_id)
        await self._tratar_resultado(task_id, resultado, argumentos, politica, trace_id, updater, responder)

    async def _continuar(self, task_id: str, texto: str, trace_id: str | None, updater: TaskUpdater, responder) -> None:
        pausa = self.pausas[task_id]
        achado = ESCOLHA.match(texto)
        valor = achado.group("valor") if achado else None
        if valor != "recusar" and valor not in pausa.alternativas:
            # Escolha fora do enum: a Task continua pausada e repete a mesma lista.
            await updater.requires_input(responder(self._linha(pausa.alternativas)))
            return
        await updater.start_work()
        resposta = {"action": "decline"} if valor == "recusar" else {"action": "accept", "content": {"sala": valor}}
        # Retry: mesmos argumentos, id de JSON-RPC novo, a mesma chave e o requestState ecoado intacto.
        resultado = await self.mcp.chamar_tool(
            TOOL_RESERVA,
            pausa.argumentos,
            trace_id or pausa.trace_id,
            input_responses={pausa.chave: resposta},
            request_state=pausa.request_state,
        )
        del self.pausas[task_id]
        await self._tratar_resultado(
            task_id, resultado, pausa.argumentos, pausa.politica, trace_id or pausa.trace_id, updater, responder
        )

    async def _tratar_resultado(
        self,
        task_id: str,
        resultado: dict[str, Any],
        argumentos: dict[str, Any],
        politica: str,
        trace_id: str,
        updater: TaskUpdater,
        responder,
    ) -> None:
        if resultado.get("resultType") == "input_required":
            # A ponte: o input_required do MCP vira a pausa da Task A2A.
            pedidos = resultado.get("inputRequests") or {}
            chave, pedido = next(iter(pedidos.items()))
            opcoes = alternativas_da(pedido)
            self.pausas[task_id] = Pausa(
                argumentos=argumentos,
                chave=chave,
                request_state=resultado["requestState"],
                alternativas=opcoes,
                politica=politica,
                trace_id=trace_id,
            )
            await updater.requires_input(responder(self._linha(opcoes)))
            return
        if resultado.get("isError"):
            await updater.failed(responder(texto_do(resultado)))
            return
        dados = resultado.get("structuredContent") or {}
        if dados.get("reservado") is False:
            await updater.cancel(responder(dados.get("motivo") or "reserva recusada"))
            return
        reserva = {
            "reserva": dados.get("reserva"),
            "sala": dados.get("sala"),
            "inicio": dados.get("inicio"),
            "fim": dados.get("fim"),
            "responsavel": dados.get("responsavel"),
            "politica": politica,
        }
        await updater.add_artifact(
            [new_text_part(json.dumps(reserva, ensure_ascii=False, separators=(",", ":")))], name="reserva"
        )
        await updater.complete(responder(f"reserva {reserva['reserva']} criada em {reserva['sala']}"))

    @staticmethod
    def _linha(alternativas: list[str]) -> str:
        return "alternativas: " + ", ".join(alternativas)

    @staticmethod
    def _versao(politica: str) -> str:
        primeira = politica.splitlines()[0] if politica else ""
        return primeira.split(":", 1)[1].strip() if ":" in primeira else ""


def agent_card() -> AgentCard:
    return AgentCard(
        name="Central de Salas",
        description="Reserva salas de reuniao da Hill Valley Tech.",
        provider=AgentProvider(organization="Hill Valley Tech", url="https://hillvalley.example"),
        version="1.0.0",
        supported_interfaces=[
            AgentInterface(url=f"{URL_PUBLICA}/a2a", protocol_binding="JSONRPC", protocol_version="1.0")
        ],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False, extended_agent_card=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[
            AgentSkill(
                id="reservar-sala",
                name="Reservar sala",
                description="Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
                tags=["salas", "agenda"],
                input_modes=["text/plain"],
                output_modes=["text/plain"],
                examples=[
                    "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 "
                    "fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
                ],
            )
        ],
    )


class ContextoV1(DefaultServerCallContextBuilder):
    """Trata request sem header `A2A-Version` como 1.0, a unica versao que o card declara.

    O SDK le header ausente como 0.3 e recusa; o card so anuncia 1.0, entao um
    cliente que o leu esta falando 1.0. Header presente com outra versao segue
    sendo recusado pelo SDK.
    """

    def build(self, request) -> ServerCallContext:
        contexto = super().build(request)
        cabecalhos = contexto.state.setdefault("headers", {})
        if not (cabecalhos.get("A2A-Version") or cabecalhos.get("a2a-version")):
            cabecalhos["a2a-version"] = "1.0"
        return contexto


def criar_app() -> Starlette:
    card = agent_card()
    mcp = ClienteMCP(MCP_URL)
    handler = DefaultRequestHandler(agent_executor=CentralDeSalas(mcp), task_store=InMemoryTaskStore(), agent_card=card)
    rotas = create_agent_card_routes(card) + create_jsonrpc_routes(handler, rpc_url="/a2a", context_builder=ContextoV1())

    @contextlib.asynccontextmanager
    async def ciclo(_app: Starlette):
        yield
        await mcp.fechar()

    return Starlette(routes=rotas, lifespan=ciclo)


def _sockets(host: str, porta: int) -> list[socket.socket]:
    """Em `localhost`, escuta em 127.0.0.1 e ::1: no Windows, um cliente que tenta
    ::1 primeiro e e recusado espera ~2 s antes de cair para IPv4."""
    enderecos = [("127.0.0.1", socket.AF_INET), ("::1", socket.AF_INET6)] if host == "localhost" else [
        (host, socket.AF_INET6 if ":" in host else socket.AF_INET)
    ]
    abertos = []
    for i, (endereco, familia) in enumerate(enderecos):
        s = socket.socket(familia, socket.SOCK_STREAM)
        try:
            if familia == socket.AF_INET6:
                s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            s.bind((endereco, porta))
        except OSError:
            s.close()
            if i == 0:
                raise  # o endereco principal tem de subir; ::1 e so um extra
            continue
        abertos.append(s)
    return abertos


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    servidor = uvicorn.Server(uvicorn.Config(criar_app(), log_level="warning"))
    servidor.run(sockets=_sockets(HOST, PORTA))


if __name__ == "__main__":
    main()
