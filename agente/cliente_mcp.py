"""Cliente MCP minimo sobre Streamable HTTP, sem sessao (revisao 2026-07-28).

Cada request carrega o proprio `_meta` (versao, capabilities e traceparent) e
os headers que o transporte espelha do corpo. O resultado volta cru: um
`input_required` nao e respondido aqui, ele sobe para quem chamou, porque e o
cliente A2A quem decide a escolha. Por isso nao ha callback de elicitation.
"""

from __future__ import annotations

import itertools
import logging
import secrets
from typing import Any

import httpx

PROTOCOLO = "2026-07-28"
CAPABILITIES = {"elicitation": {"form": {}}}
CLIENT_INFO = {"name": "agente-central-de-salas", "version": "1.0.0"}
METODOS_COM_NOME = {"tools/call": "name", "resources/read": "uri"}

log = logging.getLogger("agente.mcp")


class ErroMCP(Exception):
    """Erro de protocolo (campo `error` do JSON-RPC) devolvido pelo servidor MCP."""

    def __init__(self, codigo: int | None, mensagem: str, dados: Any = None) -> None:
        super().__init__(mensagem)
        self.codigo = codigo
        self.mensagem = mensagem
        self.dados = dados


def novo_span(trace_id: str) -> str:
    """traceparent W3C com o trace-id da Task e um span-id novo."""
    return f"00-{trace_id}-{secrets.token_hex(8)}-01"


class ClienteMCP:
    def __init__(self, url: str) -> None:
        self.url = url
        self._http = httpx.AsyncClient(timeout=30)
        # Id de JSON-RPC unico por request: o retry nunca reaproveita o id do request inicial.
        self._ids = itertools.count(1)

    async def fechar(self) -> None:
        await self._http.aclose()

    async def request(self, metodo: str, params: dict[str, Any], trace_id: str) -> dict[str, Any]:
        id_ = next(self._ids)
        traceparent = novo_span(trace_id)
        corpo = {
            "jsonrpc": "2.0",
            "id": id_,
            "method": metodo,
            "params": {
                **params,
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": PROTOCOLO,
                    "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
                    "io.modelcontextprotocol/clientCapabilities": CAPABILITIES,
                    "traceparent": traceparent,
                },
            },
        }
        cabecalhos = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOLO,
            "Mcp-Method": metodo,
        }
        if metodo in METODOS_COM_NOME:
            cabecalhos["Mcp-Name"] = str(params[METODOS_COM_NOME[metodo]])
        log.info("-> mcp %s id=%s traceparent=%s", metodo, id_, traceparent)
        resposta = await self._http.post(self.url, json=corpo, headers=cabecalhos)
        dados = resposta.json()
        if "error" in dados:
            erro = dados["error"]
            raise ErroMCP(erro.get("code"), erro.get("message", ""), erro.get("data"))
        return dados["result"]

    async def listar_tools(self, trace_id: str) -> list[dict[str, Any]]:
        return (await self.request("tools/list", {}, trace_id)).get("tools", [])

    async def ler_resource(self, uri: str, trace_id: str) -> str:
        resultado = await self.request("resources/read", {"uri": uri}, trace_id)
        return "".join(c.get("text", "") for c in resultado.get("contents", []))

    async def chamar_tool(
        self,
        nome: str,
        argumentos: dict[str, Any],
        trace_id: str,
        *,
        input_responses: dict[str, Any] | None = None,
        request_state: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"name": nome, "arguments": argumentos}
        if input_responses is not None:
            params["inputResponses"] = input_responses
        if request_state is not None:
            params["requestState"] = request_state
        return await self.request("tools/call", params, trace_id)
