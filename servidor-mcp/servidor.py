"""Servidor MCP da Central de Salas (Streamable HTTP, revisao 2026-07-28).

Expoe tres tools (listar_salas, consultar_disponibilidade, reservar_sala) e o
resource politica://uso. A reserva em conflito usa MRTR: um resolver devolve
`Elicit(...)`, o SDK responde `input_required` e sela o `requestState` com a
chave de REQUEST_STATE_SECRET. Nada fica guardado entre o pedido e o retry.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

import uvicorn
from pydantic import BaseModel, Field, create_model

from mcp.server.elicitation import AcceptedElicitation, ElicitationResult
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.resolve import Elicit, Resolve
from mcp.server.request_state import RequestStateSecurity

NOME = "central-de-salas"
VERSAO = "1.0.0"
DADOS = Path(__file__).resolve().parent.parent / "dados"
SAO_PAULO = timezone(timedelta(hours=-3))
JANELA_INICIO = time(8, 0)
JANELA_FIM = time(20, 0)
DURACAO_MAXIMA = timedelta(hours=2)
MAX_ALTERNATIVAS = 3
TTL_REQUEST_STATE = 600.0  # 10 minutos

ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"

log = logging.getLogger("servidor-mcp")


# --- dados do dominio ---------------------------------------------------------

SALAS: list[dict[str, Any]] = json.loads((DADOS / "salas.json").read_text(encoding="utf-8"))
SALAS_POR_ID = {s["id"]: s for s in SALAS}
RESERVAS: list[dict[str, Any]] = json.loads((DADOS / "reservas.json").read_text(encoding="utf-8"))
POLITICA_TEXTO = (DADOS / "politica-de-uso.md").read_text(encoding="utf-8")
POLITICA_VERSAO = POLITICA_TEXTO.splitlines()[0].split(":", 1)[1].strip()


def _instante(valor: str, campo: str) -> datetime:
    try:
        dt = datetime.fromisoformat(valor)
    except ValueError:
        raise ToolError(f"Data invalida em {campo}: {valor}") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SAO_PAULO)
    return dt.astimezone(SAO_PAULO)


def validar_pedido(sala: str, inicio: str, fim: str) -> tuple[datetime, datetime]:
    """Aplica as regras de sala e de politica; levanta ToolError com a mensagem exata."""
    if sala not in SALAS_POR_ID:
        raise ToolError(f"Sala inexistente: {sala}")
    ini, fi = _instante(inicio, "inicio"), _instante(fim, "fim")
    if fi <= ini:
        raise ToolError(ERRO_INTERVALO)
    fora = (
        ini.time() < JANELA_INICIO
        or fi.date() != ini.date()
        or fi.time() > JANELA_FIM
    )
    if fora:
        raise ToolError(ERRO_JANELA)
    if fi - ini > DURACAO_MAXIMA:
        raise ToolError(ERRO_DURACAO)
    return ini, fi


def conflitos(sala: str, ini: datetime, fi: datetime) -> list[dict[str, Any]]:
    return [
        r
        for r in RESERVAS
        if r["sala"] == sala and _instante(r["inicio"], "inicio") < fi and ini < _instante(r["fim"], "fim")
    ]


def alternativas(sala: str, ini: datetime, fi: datetime) -> list[str]:
    capacidade = SALAS_POR_ID[sala]["capacidade"]
    livres = [
        s for s in SALAS if s["id"] != sala and s["capacidade"] >= capacidade and not conflitos(s["id"], ini, fi)
    ]
    livres.sort(key=lambda s: (s["capacidade"], s["id"]))
    return [s["id"] for s in livres[:MAX_ALTERNATIVAS]]


def criar_reserva(sala: str, inicio: str, fim: str, responsavel: str) -> dict[str, Any]:
    reserva = {
        "id": f"res-{len(RESERVAS) + 1:04d}",
        "sala": sala,
        "inicio": inicio,
        "fim": fim,
        "responsavel": responsavel,
    }
    RESERVAS.append(reserva)
    return {
        "reserva": reserva["id"],
        "reservado": True,
        "sala": sala,
        "inicio": inicio,
        "fim": fim,
        "responsavel": responsavel,
        "politica": POLITICA_VERSAO,
    }


# --- log de requests no stderr ------------------------------------------------


class LogDeRequests:
    """ASGI: registra metodo, id e traceparent de todo request JSON-RPC recebido.

    Fica fora da cadeia do SDK para registrar tambem o que a borda rejeita
    (`_meta` incompleto, `requestState` adulterado, header divergente).
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        corpo = b""
        mais = True
        while mais:
            mensagem = await receive()
            corpo += mensagem.get("body", b"")
            mais = mensagem.get("more_body", False)
        self._registrar(corpo)
        entregue = False

        async def reenviar() -> dict[str, Any]:
            nonlocal entregue
            if entregue:
                return await receive()
            entregue = True
            return {"type": "http.request", "body": corpo, "more_body": False}

        await self.app(scope, reenviar, send)

    @staticmethod
    def _registrar(corpo: bytes) -> None:
        try:
            dados = json.loads(corpo)
        except ValueError:
            log.info("request corpo-invalido")
            return
        for item in dados if isinstance(dados, list) else [dados]:
            if not isinstance(item, dict):
                continue
            params = item.get("params") if isinstance(item.get("params"), dict) else {}
            meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
            log.info(
                "request method=%s id=%s alvo=%s traceparent=%s retry=%s",
                item.get("method"),
                item.get("id"),
                params.get("name") or params.get("uri") or "-",
                meta.get("traceparent", "-"),
                "sim" if params.get("requestState") else "nao",
            )


# --- servidor -----------------------------------------------------------------


def _segredo() -> str:
    segredo = os.environ.get("REQUEST_STATE_SECRET", "")
    if len(segredo.encode()) < 32:
        sys.exit(
            "REQUEST_STATE_SECRET ausente ou com menos de 32 bytes. Gere com:\n"
            '  python -c "import secrets; print(secrets.token_hex(32))"'
        )
    return segredo


mcp = MCPServer(
    name=NOME,
    version=VERSAO,
    request_state_security=RequestStateSecurity(keys=[_segredo()], ttl=TTL_REQUEST_STATE),
)


class Sala(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[Sala]


@mcp.tool(description="Lista as salas de reuniao com capacidade e recursos.")
def listar_salas() -> ListaDeSalas:
    return ListaDeSalas(salas=[Sala(**s) for s in SALAS])


@mcp.tool(description="Informa se a sala esta livre no intervalo e, se nao estiver, as reservas em conflito.")
def consultar_disponibilidade(
    sala: Annotated[str, Field(description="id da sala, como em listar_salas")],
    inicio: Annotated[str, Field(description="inicio em ISO 8601 com offset")],
    fim: Annotated[str, Field(description="fim em ISO 8601 com offset")],
) -> dict[str, Any]:
    ini, fi = validar_pedido(sala, inicio, fim)
    em_conflito = conflitos(sala, ini, fi)
    return {"sala": sala, "inicio": inicio, "fim": fim, "livre": not em_conflito, "conflitos": em_conflito}


def escolha_de_sala(sala: str, inicio: str, fim: str) -> Elicit[Any] | None:
    """Resolver do MRTR: valida o pedido e, havendo conflito, pede a escolha de uma alternativa.

    Roda de novo a cada round; a resposta do cliente so e aceita se a pergunta
    (o enum de alternativas) for a mesma que foi feita.
    """
    ini, fi = validar_pedido(sala, inicio, fim)
    if not conflitos(sala, ini, fi):
        return None
    opcoes = alternativas(sala, ini, fi)
    if not opcoes:
        raise ToolError(ERRO_SEM_ALTERNATIVA)
    esquema = create_model(
        "EscolhaDeSala",
        sala=(Literal[tuple(opcoes)], Field(title="Sala", description="Sala alternativa escolhida")),
    )
    return Elicit("A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.", esquema)


@mcp.tool(description="Reserva uma sala. Em conflito, pede a escolha de uma sala alternativa (MRTR).")
def reservar_sala(
    sala: Annotated[str, Field(description="id da sala, como em listar_salas")],
    inicio: Annotated[str, Field(description="inicio em ISO 8601 com offset")],
    fim: Annotated[str, Field(description="fim em ISO 8601 com offset")],
    responsavel: Annotated[str, Field(description="nome de quem reserva")],
    escolha: Annotated[ElicitationResult[Any], Resolve(escolha_de_sala)],
) -> dict[str, Any]:
    if not isinstance(escolha, AcceptedElicitation):
        return {"reservado": False, "motivo": f"usuario recusou as alternativas ({escolha.action})", "sala": sala}
    sala_final = sala if escolha.data is None else escolha.data.sala
    return criar_reserva(sala_final, inicio, fim, responsavel)


@mcp.resource("politica://uso", name="politica-de-uso", mime_type="text/markdown",
              description="Politica de uso das salas; a primeira linha declara a versao.")
def politica_de_uso() -> str:
    return POLITICA_TEXTO


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
    porta = int(os.environ.get("MCP_PORT", "7301"))
    app = mcp.streamable_http_app(streamable_http_path="/mcp", json_response=True, stateless_http=True)
    host = os.environ.get("MCP_HOST", "localhost")
    servidor = uvicorn.Server(uvicorn.Config(LogDeRequests(app), log_level="warning"))
    servidor.run(sockets=_sockets(host, porta))


if __name__ == "__main__":
    main()
