# A Ponte: um agente A2A com MCP por dentro

Entrega do desafio do curso de MCP e A2A (MBA Engenharia de Software com IA). São dois processos separados que conversam por HTTP:

- **`servidor-mcp/`**: servidor MCP da Central de Salas, em Streamable HTTP na porta `7301` (`/mcp`), feito com o SDK oficial `mcp==2.3.0`, revisão `2026-07-28` da spec. Tem três tools (`listar_salas`, `consultar_disponibilidade`, `reservar_sala`), o resource `politica://uso` e o ciclo completo de MRTR na reserva.
- **`agente/`**: agente que é host MCP por dentro e servidor A2A v1.0 por fora, com JSON-RPC na porta `7300` (`/a2a`) e o card em `/.well-known/agent-card.json`. Feito com `a2a-sdk==1.3.0`. Não usa LLM: interpreta o pedido em formato fixo e decide por regra.

O enunciado original do desafio está no repositório base: https://github.com/devfullcycle/desafio-a2a-com-mcp

## Como rodar

Pré-requisito: Python 3.10 ou superior. Os comandos abaixo partem da raiz de um clone limpo. Use três terminais: um para o servidor MCP, um para o agente e um para o validador.

**1. Ambiente e dependências** (uma vez só; as versões estão travadas em `servidor-mcp/pyproject.toml` e `agente/pyproject.toml`):

```bash
python3 -m venv .venv
.venv/bin/pip install ./servidor-mcp ./agente
```

No Windows (PowerShell), troque `.venv/bin/` por `.venv\Scripts\`:

```powershell
python -m venv .venv
.venv\Scripts\pip install ./servidor-mcp ./agente
```

**2. Segredo do `requestState`.** O servidor MCP lê a chave de integridade da variável `REQUEST_STATE_SECRET` e não sobe sem ela, ou com menos de 32 bytes. Gere a sua no terminal do servidor MCP e não a coloque em nenhum arquivo versionado:

```bash
export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
```

```powershell
$env:REQUEST_STATE_SECRET = python -c "import secrets; print(secrets.token_hex(32))"
```

Para testar o `requestState` depois de um restart do servidor, reinicie **no mesmo terminal**, ou exporte o mesmo valor de novo. Uma chave nova invalida os estados emitidos com a anterior, e isso é intencional.

**3. Terminal 1, servidor MCP** (o log de cada request sai no stderr):

```bash
.venv/bin/python servidor-mcp/servidor.py
```

**4. Terminal 2, agente:**

```bash
.venv/bin/python agente/agente.py
```

**5. Terminal 3, validador** (com os dois processos recém-iniciados):

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

Variáveis opcionais, com os valores padrão entre parênteses: `MCP_PORT` (`7301`), `MCP_HOST` (`localhost`), `AGENTE_PORT` (`7300`), `AGENTE_HOST` (`localhost`), `AGENTE_URL` (`http://127.0.0.1:7300`, a URL publicada no card) e `MCP_URL` (`http://127.0.0.1:7301/mcp`, o servidor MCP que o agente consome). No host padrão `localhost`, os dois processos escutam em `127.0.0.1` e em `::1`. Sem isso, no Windows, cada request de um cliente que tenta IPv6 primeiro esperava cerca de 2 s pela recusa antes de cair para IPv4.

Para ver a ponte à mão, com o agente de pé:

```bash
curl -s http://localhost:7300/a2a -H 'Content-Type: application/json' -H 'traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01' \
  -d '{"jsonrpc":"2.0","id":1,"method":"SendMessage","params":{"message":{"messageId":"m1","role":"ROLE_USER","parts":[{"text":"reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty"}]}}}'
# -> TASK_STATE_INPUT_REQUIRED com "alternativas: sala-fusca, sala-mirante"
curl -s http://localhost:7300/a2a -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"SendMessage","params":{"message":{"messageId":"m2","role":"ROLE_USER","taskId":"<id da task>","parts":[{"text":"escolha=sala-mirante"}]}}}'
# -> TASK_STATE_COMPLETED com o artifact "reserva"
```

## Onde a ponte acontece

A ponte está em `agente/agente.py`, na classe `CentralDeSalas`.

**Ida.** Em `_tratar_resultado` (`agente/agente.py:180`), quando o `tools/call` de `reservar_sala` volta com `resultType` igual a `input_required`, o agente:

1. lê a única entrada de `inputRequests`, ou seja, a chave atribuída pelo servidor e o `enum` do `requestedSchema`;
2. guarda numa `Pausa` ligada ao `task_id` os argumentos originais, a chave, o `requestState` (sem abrir nem interpretar) e as alternativas (`agente/agente.py:195`);
3. publica `TASK_STATE_INPUT_REQUIRED` com a linha `alternativas: ...`, na ordem do `enum`.

O `requestState` nunca sai do agente: não aparece no card, no artifact nem em mensagem nenhuma.

**Volta.** Em `_continuar` (`agente/agente.py:157`), o `SendMessage` de continuação com `taskId` e `escolha=<id>` (ou `escolha=recusar`) vira um novo `tools/call` com:

- os mesmos argumentos;
- `inputResponses`, com a mesma chave: `accept` com `{"sala": ...}`, ou `decline`;
- o `requestState` ecoado byte a byte (`agente/agente.py:173`).

O id de JSON-RPC é sempre novo, porque `ClienteMCP` numera todo request por um contador (`agente/cliente_mcp.py:46`). Uma escolha fora do `enum` não chega ao MCP: a Task continua pausada e a mesma lista é repetida.

**No servidor.** O ciclo é o MRTR de primeira classe do SDK:

1. O resolver `escolha_de_sala` (`servidor-mcp/servidor.py:226`) valida o pedido. Havendo conflito, calcula as alternativas e devolve `Elicit(...)` com um schema de `enum` dinâmico.
2. O SDK transforma isso em `input_required`, depois de checar se o cliente declarou elicitation em form mode (senão responde `-32021`), e sela o `requestState`.
3. No retry, o SDK verifica o selo, roda o resolver de novo e entrega a resposta a `reservar_sala` (`servidor-mcp/servidor.py:246`), que cria a reserva ou devolve `reservado: false` na recusa.

## Decisões técnicas

**Proteção do `requestState`.** Uso o `RequestStateSecurity` do próprio SDK (`servidor-mcp/servidor.py:195`), com AES-256-GCM sobre uma chave derivada por HKDF-SHA256 de `REQUEST_STATE_SECRET`. O token é cifrado e autenticado (AEAD), então trocar um caractere dele faz a verificação falhar e o servidor responde `-32602` (`Invalid or expired requestState`). O envelope selado também prende o estado ao request de origem: método, nome da tool e um digest dos `arguments`. Por isso um retry com argumentos adulterados é rejeitado com `-32602` e não produz reserva. O servidor não guarda nada entre o `input_required` e o retry, e a chave vem do ambiente, não da memória do processo: um retry apresentado depois de reiniciar o servidor (com o mesmo segredo) conclui normalmente. Isso foi testado reiniciando o processo entre os dois requests.

**Validade.** O `requestState` vale 10 minutos (`TTL_REQUEST_STATE = 600`). Depois disso é rejeitado com `-32602`.

**Estado das Tasks.** As Tasks A2A ficam no `InMemoryTaskStore` do `a2a-sdk`. O estado da pausa (a `Pausa` com a chave e o `requestState`) fica num dicionário em memória do agente, `CentralDeSalas.pausas`, indexado pelo `task_id`. Por isso duas Tasks pausadas ao mesmo tempo nunca trocam de `requestState`. Ao concluir, cancelar ou falhar, a entrada é removida. Nada disso sobrevive a um restart do agente, o que o enunciado aceita. As reservas do servidor MCP também ficam em memória, partindo de `dados/reservas.json`.

**Cliente MCP explícito no agente.** O agente fala MCP por um cliente HTTP próprio e pequeno (`agente/cliente_mcp.py`, sobre `httpx`). Cada request leva:

- no `_meta`: `protocolVersion`, `clientCapabilities` exatamente `{"elicitation": {"form": {}}}` e um `traceparent` com o trace-id da Task e um span-id novo;
- nos headers: `MCP-Protocol-Version`, `Mcp-Method` e `Mcp-Name`, este último em `tools/call` e `resources/read`.

O resultado volta cru. Não usei o `Client` do SDK porque ele só declara elicitation quando recebe um callback, e nesse caso declara também o modo URL. Além disso, por padrão ele fecha o ciclo de MRTR sozinho, e aí a Task nunca pausaria. Para cada Task nova, o agente faz `tools/list` antes do primeiro `tools/call`, sem lista fixa de tools no código, e lê `politica://uso` para extrair a versão que vai no campo `politica` do artifact.

**`traceparent`.** O trace-id do header `traceparent` da chamada A2A é guardado com a Task e propagado em todos os requests MCP daquela Task, inclusive no retry de uma continuação que chegue sem o header. Se a chamada A2A não traz `traceparent`, o agente gera um trace-id para a Task. O servidor MCP registra no stderr, para todo request recebido, o método, o id, o alvo, o `traceparent` e se é um retry. O log fica num middleware ASGI fora da cadeia do SDK, então também registra o que a borda rejeita, como `_meta` incompleto e `requestState` adulterado.

**Header `A2A-Version`.** O `a2a-sdk` interpreta a ausência do header `A2A-Version` como versão `0.3` e recusa a chamada. O validador não envia esse header. Como o card só anuncia a interface `1.0`, o agente trata um request sem o header como `1.0` (`ContextoV1` em `agente/agente.py`). Um header presente com outra versão continua sendo recusado pelo SDK.

**Regras de domínio só no servidor.** Conflito, janela de uso, duração, intervalo e cálculo de alternativas estão todos em `servidor-mcp/servidor.py`. O agente só traduz protocolo. A única checagem que ele faz sobre a escolha é se o valor está no `enum` que o próprio servidor mandou.

**Ruído conhecido no log do agente.** De vez em quando a telemetria do `a2a-sdk` registra `Failed to detach context` (OpenTelemetry) quando o loop de eventos de uma Task é coletado. Não afeta nenhuma resposta e acontece dentro do SDK.

## Saída do validador

Última execução, com os dois processos recém-iniciados:

```
trace-id desta execucao: 43d8fca5f55ca4178b31abca942bfcf4
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```

Trecho do stderr do servidor MCP na mesma execução. Mostra o `tools/list` antes do primeiro `tools/call` do agente, o trace-id do validador propagado, e o par pausa/retry com ids diferentes (6 e 7):

```
request method=tools/list id=1 alvo=- traceparent=00-43d8fca5f55ca4178b31abca942bfcf4-67fbe6082dce5abb-01 retry=nao
request method=resources/read id=2 alvo=politica://uso traceparent=00-43d8fca5f55ca4178b31abca942bfcf4-491cfef2afd0d65a-01 retry=nao
request method=tools/call id=3 alvo=reservar_sala traceparent=00-43d8fca5f55ca4178b31abca942bfcf4-ac27172f9dae3da9-01 retry=nao
...
request method=tools/call id=6 alvo=reservar_sala traceparent=00-43d8fca5f55ca4178b31abca942bfcf4-ea77c3351bfd99d9-01 retry=nao
request method=tools/call id=7 alvo=reservar_sala traceparent=00-43d8fca5f55ca4178b31abca942bfcf4-1452cad9523eac6f-01 retry=sim
```
