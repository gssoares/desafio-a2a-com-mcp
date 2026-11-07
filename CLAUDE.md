# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

MBA challenge "A Ponte": build two **separate processes** that talk over HTTP:

- `servidor-mcp/` — MCP server (Streamable HTTP, port `7301`, endpoint `/mcp`) exposing meeting-room tools.
- `agente/` — an agent that is an **MCP host/client inside** (calls the server above over HTTP) and an **A2A v1.0 server outside** (JSON-RPC 2.0 at `:7300/a2a`, card at `/.well-known/agent-card.json`).

Stack: Python, one shared `.venv` at the repo root. `servidor-mcp/servidor.py` uses `mcp==2.3.0` (`MCPServer`); `agente/agente.py` uses `a2a-sdk[http-server]==1.3.0`, and `agente/cliente_mcp.py` is a small hand-written MCP-over-HTTP client (`httpx`). Versions are pinned in each folder's `pyproject.toml`.

`README.md` is the **delivery doc** (in Portuguese), with four required sections: how to run, where the bridge happens (cites `file:line`, so keep it in sync when moving code), technical decisions, and the full validator output (paste a fresh run after behavior changes). The original challenge spec is upstream: https://github.com/devfullcycle/desafio-a2a-com-mcp. `git show 263f17e:README.md` shows it too.

## Hard constraints

- **Never modify `dados/`, `validador/`, or `exemplos/`.** The grader runs the validator from the original repo.
- Required SDK: official MCP SDK v2 aligned with spec revision `2026-07-28` — `@modelcontextprotocol/server` (Node 20+, ESM) or Python `mcp` package (3.10+). A2A v1.0, JSON-RPC binding. Pin exact versions in `package.json` / `pyproject.toml`. Fetch current SDK docs (ctx7) rather than relying on memory — MRTR APIs are new (`input_required` in Python SDK, `inputRequired` in TS SDK).
- No LLM anywhere in the agent's execution path, and no LLM provider SDK in dependencies. Agent is deterministic and parses a fixed format.
- The agent must not implement room business rules (conflicts, policy, alternatives) — those live only in the MCP server. The agent must not import server code.
- No protocol session on either side: every MCP request carries its own `_meta`; never infer version/capabilities from earlier requests.
- `requestState` is opaque to the agent: store and echo verbatim, never decode.
- Reservations may be in-memory; `requestState` must survive an MCP server restart (all state travels inside it).
- Out of scope: auth, streaming/SSE/push, OpenTelemetry spans, persistence, UI, containers.

## Commands

```bash
python -m venv .venv && .venv/Scripts/pip install ./servidor-mcp ./agente   # Windows; .venv/bin/ elsewhere
export REQUEST_STATE_SECRET=$(python -c "import secrets; print(secrets.token_hex(32))")
.venv/Scripts/python servidor-mcp/servidor.py   # terminal 1, logs every request to stderr
.venv/Scripts/python agente/agente.py           # terminal 2
```

Don't wait for readiness with `curl http://.../mcp`. A GET there opens an SSE stream and hangs. Probe `/.well-known/agent-card.json` on the agent, or open a TCP connection to 7301. To test restart survival, restart the MCP server with the **same** `REQUEST_STATE_SECRET`.

Validator (stdlib-only Python 3.10+, 36 checks: 1–20 hit MCP directly, 21–36 hit the agent via A2A):

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
# optional: --caminho-mcp /mcp --caminho-a2a /a2a
```

**Restart both processes before every validator run** — reservations created by one run change the next run's results (false negatives otherwise). The validator prints the trace-id it uses on its first line; it should appear in the MCP server's stderr.

There is no way to run a single check: the checks are a stateful sequence sharing one in-memory reservation set (e.g. check 24 books `sala-porao` 09–10 so check 36 can hit a conflict; checks 27–33 depend on earlier bookings for the `alternativas: sala-fusca, sala-mirante` ordering). To debug one check, read its block in `validador/validar.py` and reproduce it with curl using the matching `exemplos/wire/` body. Two requirements are not covered by the validator and must be checked by hand: `requestState` still valid after an MCP server restart, and `traceparent` showing up in the server's stderr.

The MCP server requires `REQUEST_STATE_SECRET` (≥32 random bytes, from env, never hardcoded/committed):

```bash
export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
```

Ports may be env-configurable but must default to 7300 (agent) / 7301 (MCP).

## Domain (fixed data in `dados/`)

- `salas.json` — 5 rooms (`id`, `nome`, `capacidade`, `recursos`); `reservas.json` — preloaded reservations; `politica-de-uso.md` — exposed as resource `politica://uso` (`text/markdown`); its first line `versao: 2026-11-01` must be extracted by the agent and put in the artifact's `politica` field.
- Policy: 08:00–20:00 at -03:00, max 2h, no overlap. No rule depends on today's date.
- Alternatives on conflict: rooms free in that interval with capacity ≥ requested room, max 3, sorted by capacity asc then `id` asc.

Exact error strings are checked byte-for-byte (SDK prefixes like `Error executing tool X:` are tolerated). Copy them from the constants at the top of `validador/validar.py`.

## How the implementation maps onto the SDKs

- **Server MRTR:** `reservar_sala` has a parameter `escolha: Annotated[ElicitationResult[Any], Resolve(escolha_de_sala)]`. The resolver validates the request, and on conflict returns `Elicit(msg, <pydantic model with sala: Literal[*alternativas]>)`. The SDK then does the rest:
  - emits the `input_required` reply (key = `module:qualname` of the resolver);
  - checks the client's form-elicitation capability and answers `-32021` if it is missing;
  - seals and verifies `requestState`, rejecting tampered state and argument mismatches with `-32602`.

  Resolvers re-run on every round: an answer is consumed only if the rendered question (the enum) is unchanged. All domain errors are raised as `ToolError`, which the client sees as `isError`.
- **Server sealing:** `RequestStateSecurity(keys=[REQUEST_STATE_SECRET], ttl=600)` uses AES-GCM. The sealed envelope binds the method, the tool name and a digest of the arguments. Request logging is an ASGI wrapper (`LogDeRequests`), not SDK middleware, so it also catches requests the SDK's boundary rejects.
- **Agent:** `CentralDeSalas(AgentExecutor)` publishes Task events via `TaskUpdater`, and keeps paused state in `self.pausas[task_id]`. The incoming `traceparent` is read from `context.call_context.state["headers"]`. `ContextoV1` is needed because `a2a-sdk` treats a missing `A2A-Version` header as 0.3 and rejects it, and the validator never sends that header.
- **Agent MCP client:** the agent deliberately does not use the SDK's `Client`. That client only declares elicitation when given a callback, which also declares URL mode, and by default it auto-drives MRTR.
- **Networking:** both processes bind `127.0.0.1` and `::1` when the host is `localhost` (helper `_sockets`). Otherwise, on Windows, the validator's `localhost` requests wait ~2 s for the IPv6 refusal before falling back.
- **Known noise:** `a2a-sdk` telemetry occasionally logs OpenTelemetry `Failed to detach context` tracebacks. They are harmless.

## Architecture: the "bridge"

The core of the challenge is mapping MCP MRTR state onto A2A Task state:

1. A2A `SendMessage` (text `reservar sala=<id> inicio=<iso> fim=<iso> responsavel=<nome>`) opens a Task (SUBMITTED → WORKING).
2. Agent does `tools/list` (runtime discovery, no hardcoded tool list) and `resources/read politica://uso`, then `tools/call reservar_sala`.
3. On conflict the server returns `resultType: "input_required"` with a single-entry `inputRequests` map (server-chosen key → `elicitation/create`, `mode: form`, flat `requestedSchema` with `sala` enum/const of alternatives) plus a signed `requestState` (HMAC/AEAD, 5–30 min expiry). The server keeps nothing in memory between calls.
4. Agent sets Task to `TASK_STATE_INPUT_REQUIRED` with status message exactly `alternativas: <id1>, <id2>` (enum order, nothing else) and stores `requestState` + input key per Task. `requestState` must never appear in any A2A response.
5. Continuation `SendMessage` with `taskId` and `escolha=<id>` / `escolha=recusar`. Invalid choice → stay INPUT_REQUIRED and repeat the list. Otherwise retry the original `tools/call` with a **new JSON-RPC id**, `params.inputResponses` (same key; `accept`+`{sala}` or `decline`) and the echoed `requestState`.
6. Complete → `TASK_STATE_COMPLETED` with artifact `name: "reserva"` (JSON text of the reservation incl. `politica`); decline → `TASK_STATE_CANCELED`; tool `isError` → `TASK_STATE_FAILED` with the exact tool message in history. Terminal tasks reject further `SendMessage` with an error.

Critical pitfalls (from the spec):
- MRTR is not a callback: the server never initiates requests (stateless transport has no back-channel). Don't register an elicitation callback in the agent's MCP client — the agent must see the raw `input_required` result so the Task can pause.
- Agent MCP requests must include `_meta` with `io.modelcontextprotocol/protocolVersion: "2026-07-28"`, `io.modelcontextprotocol/clientCapabilities: {"elicitation": {"form": {}}}`, and `traceparent` (same trace-id as the incoming A2A `traceparent` header; span-id may change). Also HTTP headers `MCP-Protocol-Version`, `Mcp-Method`, and `Mcp-Name` (for `tools/call`/`resources/read`); header/body mismatch → `-32020`.
- Server protocol errors: missing `_meta` fields → `-32602` + HTTP 400; elicitation needed but client lacks form-mode capability → `-32021` with `data.requiredCapabilities` + HTTP 400; tampered/expired `requestState` → `-32602`; unknown resource URI → `-32602` (never empty `contents`); unknown tool → `-32602` or `isError`.
- On retry, client-sent `arguments` are untrusted: use sealed values or reject on mismatch.
- Server logs each request to stderr with method, id, and `_meta.traceparent`.
- Paused state is per Task: two concurrent paused Tasks must each complete with their own `requestState`.
- No streaming, so `SendMessage` must run the MCP calls synchronously and return the Task (`result.task`) already in its paused or terminal state; the validator reads state straight from the `SendMessage` response. `GetTask` params are `{"id": <taskId>}`; continuation messages carry `message.taskId`. Roles are `ROLE_USER` / `ROLE_AGENT`.
- How the validator reads the card: it looks for `supportedInterfaces[]` with `protocolBinding` starting with `JSONRPC`, a `url`, and `protocolVersion` `1.0`.
- Leak check: the validator searches every A2A response body for the first 40 characters of a real `requestState`. Don't log it into messages or history.
- Check 36 (determinism) sends the same conflicting request twice and compares the pause messages byte for byte, so the `alternativas:` line must not include anything request-specific.

`exemplos/wire/*.json` holds the exact request/response shapes (MCP tools/list, call, conflict, retry, decline, -32021, resource read; A2A agent card, SendMessage, GetTask, continuation). Read them before implementing a message shape; `07-a2a-agent-card.json` is the reference card (skill id `reservar-sala`, JSON-RPC interface, protocol version `1.0`).
