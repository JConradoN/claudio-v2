# ARCHITECTURE — Cláudio v2

**Status:** Rascunho  
**Versão:** 0.1  
**Data:** 2026-06-14  
**Referência:** [PRD.md](./PRD.md)

---

## 1. Visão Geral

```
┌─────────────────────────────────────────────────────────────────┐
│                        CANAIS DE ENTRADA                        │
│   Telegram      HTTP API :18790      MCP Server     MCP Client  │
└────────────────────────┬────────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────────┐
│                    INTENT CLASSIFIER                            │
│   1ª camada: heurística (regex/keywords, <1ms, zero LLM)        │
│   2ª camada: 27b LLM fallback (raro, ~20% dos casos)           │
│   Saída: IntentResult{type, tools[], agent?, context_hints}     │
└────────────────────────┬────────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────────┐
│                    CONTEXT BUILDER                              │
│   identity_block (~100 tokens, estático)                        │
│   + mem0 retrieval (top 5 fragmentos, ~300 tokens)              │
│   + kuzu query (entidades relacionadas, ~100 tokens)            │
│   + project_context (CLAUDE.md ativo, se houver)               │
│   + intent_instructions (~100 tokens)                           │
│   ─────────────────────────────────────────────                 │
│   Total alvo: < 600 tokens de system prompt                     │
└────────────────────────┬────────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────────┐
│              MODEL MANAGER + AgentForge EXECUTOR                │
│   Garante 27b na VRAM antes de executar                         │
│   Despacha para AgentForge com tools selecionadas (1-3)         │
│   Resposta direta ou delegação a sub-agente                     │
└────────────────────────┬────────────────────────────────────────┘
                         │
         ┌───────────────┴───────────────┐
         │                               │
┌────────▼────────┐           ┌──────────▼──────────┐
│  CHANNEL OUTPUT │           │  BACKGROUND WORKER  │
│  Telegram send  │           │  Extração de memória │
│  HTTP response  │           │  Kuzu update         │
│  MCP response   │           │  Cron runner         │
└─────────────────┘           └─────────────────────┘
         │                               │
┌────────▼───────────────────────────────▼──────────┐
│                  OBSERVABILIDADE                   │
│   runlog (~/.claudio/logs/claudio.log)             │
│   agent_mesh audit_log (~/.agent-mesh/state.db)   │
└────────────────────────────────────────────────────┘
```

---

## 2. Estrutura de Arquivos

```
claudio-v2/
├── claudio/
│   ├── main.py                  # entrypoint, DI wiring, lifecycle systemd
│   ├── config.py                # carregamento de config (~/.claudio/config.json)
│   ├── channels/
│   │   ├── telegram.py          # bot handler, rotas, whitelist
│   │   ├── chatapi.py           # FastAPI: POST /api/chat, GET /api/health, SSE
│   │   └── mcp_server.py        # MCP server: ask_claudio, run_agent
│   ├── core/
│   │   ├── classifier.py        # intent classifier (heurística + 27b fallback)
│   │   ├── context.py           # context builder (identity + mem0 + kuzu + project)
│   │   ├── executor.py          # wrapper AgentForge runtime
│   │   └── model_manager.py     # Ollama state tracker, unload/load explícito
│   ├── memory/
│   │   ├── retrieval.py         # busca mem0 + kuzu, monta bloco de contexto
│   │   ├── extraction.py        # extração pós-sessão via 27b
│   │   └── migration.py         # import único ~/.aurelia/memory/*.md → mem0
│   ├── cron/
│   │   ├── store.py             # SQLite: tabelas jobs + history
│   │   ├── fast_parse.py        # regex fast-path (portado do Go)
│   │   └── scheduler.py         # loop asyncio, trigger a cada 30s
│   ├── security/
│   │   └── profiles.py          # perfis chat/read/execute/privileged
│   └── audit/
│       ├── runlog.py            # arquivo rotativo, 30 dias
│       └── agent_mesh.py        # INSERT audit_log no state.db
├── agents/                      # AgentForge agent definitions (já existe no framework)
├── docs/
│   ├── PRD.md
│   └── ARCHITECTURE.md
└── pyproject.toml
```

---

## 3. Componentes em Detalhe

### 3.1 Intent Classifier

**Responsabilidade:** determinar o que o usuário quer antes de qualquer LLM call pesado.

**Camada 1 — Heurística (resolve ~80%):**
```python
@dataclass(frozen=True)
class IntentResult:
    type: str          # "command", "chat", "delegate", "execute", "research"
    tools: list[str]   # tools a ativar (1-3)
    agent: str | None  # sub-agente a delegar, se aplicável
    context_hints: list[str]  # hints para o context builder

def classify_heuristic(text: str) -> IntentResult | None:
    # Portado de MatchCommand (Aurelia/Go)
    # Retorna None se ambíguo → passa para camada 2
```

Padrões identificados na arqueologia do Aurelia e adaptados:

| Padrão | Intent | Tools |
|---|---|---|
| "agenda / me lembra / agende" | `cron_create` | — |
| "meus agendamentos / o que ta agendado" | `cron_list` | — |
| "cancela o agendamento" | `cron_cancel` | — |
| "nova conversa / reset / limpa contexto" | `session_reset` | — |
| "status / ta funcionando" | `status` | — |
| "docker / systemctl / ps / df" | `execute` | `run_bash` |
| "lê / mostra / cat / ls" | `read` | `read_file`, `run_bash` |
| "/agente / /canal / /pesquisa" | `delegate` | `run_agent` |
| mensagem curta sem padrão (<50 chars) | `chat` | — |

**Camada 2 — 27b LLM fallback (~20%):**
```python
CLASSIFIER_PROMPT = """
Classifique a intenção em uma palavra:
chat / execute / research / delegate / cron / status

Mensagem: {text}
Resposta (apenas a palavra):
"""
# Max tokens: 5. Temperatura: 0.
```

---

### 3.2 Context Builder

**Responsabilidade:** montar o system prompt final com menos de 600 tokens.

```python
def build_context(intent: IntentResult, session: Session) -> str:
    blocks = [
        IDENTITY_BLOCK,                          # ~100 tokens, estático
        retrieve_memory(intent, session),        # ~300 tokens, mem0 + kuzu
        load_project_context(session.project),   # ~100 tokens, CLAUDE.md ativo
        intent_instructions(intent),             # ~100 tokens
    ]
    return "\n\n".join(b for b in blocks if b)
```

**`IDENTITY_BLOCK`** — estático, carregado uma vez:
```
Você é Cláudio, assistente pessoal do Conrado Nogueira.
Executa no fox-server (Ubuntu, Xeon E5-2696v3, 2×RTX 3060).
Responde em PT-BR. Direto, técnico, sem rodeios.
Confirma antes de ações destrutivas.
```

**`retrieve_memory()`:**
```python
def retrieve_memory(intent: IntentResult, session: Session) -> str:
    # 1. embed a query (intent.type + context_hints + últimas 2 msgs)
    # 2. mem0.search(query, limit=5)
    # 3. kuzu: entidades mencionadas no texto
    # 4. monta bloco < 300 tokens, corta se necessário
```

---

### 3.3 Model Manager

**Responsabilidade:** garantir que apenas um modelo esteja na VRAM a cada momento.

```python
class ModelManager:
    _current: str | None = None  # modelo atualmente carregado
    _lock: asyncio.Lock

    async def ensure(self, model: str) -> None:
        async with self._lock:
            if self._current == model:
                return
            if self._current is not None:
                await self._unload(self._current)
            await self._load(model)
            self._current = model

    async def _unload(self, model: str) -> None:
        # POST /api/generate com keep_alive=0
        # ou: subprocess ollama stop <model>
        await ollama_api("POST", "/api/generate",
                         {"model": model, "keep_alive": 0})

    async def _load(self, model: str) -> None:
        # Warm-up: inference vazia para pre-carregar na VRAM
        await ollama_api("POST", "/api/generate",
                         {"model": model, "prompt": "", "keep_alive": "1h"})
```

**Estado persistido em memória do processo** — se o serviço reinicia, assume `_current = None` e verifica o Ollama via `GET /api/ps` no startup.

---

### 3.4 Executor (AgentForge wrapper)

**Responsabilidade:** despachar o request para o AgentForge com as tools selecionadas.

```python
async def execute(
    context: str,
    user_message: str,
    tools: list[str],
    session: Session,
    security_profile: str,
) -> AsyncIterator[str]:
    agent_config = build_ephemeral_agent(
        system_prompt=context,
        tools=filter_tools_by_profile(tools, security_profile),
        model="qwen3.5:27b",
        timeout=900,
    )
    async for chunk in agentforge.run_stream(agent_config, user_message):
        yield chunk
```

Tools disponíveis por perfil:

| Tool | chat | read | execute | privileged |
|---|---|---|---|---|
| `run_bash` (read-only cmds) | — | ✓ | ✓ | ✓ |
| `run_bash` (destrutivos) | — | — | — | ✓ + confirm |
| `read_file` | — | ✓ | ✓ | ✓ |
| `write_file` | — | — | ✓ | ✓ |
| `http_get` | — | ✓ | ✓ | ✓ |
| `run_agent` | — | — | ✓ | ✓ |
| `send_claudio` | ✓ | ✓ | ✓ | ✓ |

---

### 3.5 Memory System

**Fluxo de recuperação (pré-execução):**
```
text + intent
  → embed (nomic-embed-text via Ollama)
  → mem0.search(embedding, limit=5)
  → kuzu: MATCH (e:Entity) WHERE e.name IN mentions RETURN e, e.relations
  → formata bloco < 300 tokens
```

**Fluxo de extração (pós-sessão, background):**
```python
async def extract_memory(session: Session) -> None:
    prompt = f"""
    Extrai fatos relevantes desta conversa para memória permanente.
    Formato: lista de fatos curtos, um por linha.
    Apenas fatos novos, decisões, preferências ou correções.
    
    Conversa:
    {session.transcript}
    """
    # Usa o modelo que já está na VRAM (não faz swap)
    facts = await model_manager.generate(prompt, max_tokens=500)
    for fact in parse_facts(facts):
        await mem0.add(fact, user_id="conrado")
    await kuzu.update_entities_from_session(session)
```

**Migração única do Aurelia:**
```python
async def migrate_aurelia_memory() -> None:
    # Roda apenas se ~/.claudio/migration.done não existir
    memory_dir = Path("~/.aurelia/memory").expanduser()
    for md_file in memory_dir.glob("**/*.md"):
        content = md_file.read_text()
        await mem0.add(content, user_id="conrado",
                       metadata={"source": "aurelia_migration",
                                 "file": md_file.name})
    Path("~/.claudio/migration.done").touch()
```

---

### 3.6 Cron Scheduler

**Schema SQLite (`~/.claudio/cron.db`):**
```sql
CREATE TABLE jobs (
    id          TEXT PRIMARY KEY,   -- UUID
    type        TEXT NOT NULL,      -- 'once' | 'recurring'
    chat_id     INTEGER NOT NULL,
    user_id     TEXT NOT NULL,
    prompt      TEXT NOT NULL,
    cron_expr   TEXT,               -- NULL para 'once'
    run_at      TEXT,               -- NULL para 'recurring' (ISO 8601)
    active      INTEGER DEFAULT 1,
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE job_history (
    id          TEXT PRIMARY KEY,
    job_id      TEXT NOT NULL,
    ran_at      TEXT NOT NULL,
    status      TEXT NOT NULL,      -- 'ok' | 'error'
    error       TEXT
);
```

**Scheduler loop:**
```python
async def scheduler_loop() -> None:
    while True:
        now = datetime.utcnow()
        jobs = await store.due_jobs(now)
        for job in jobs:
            asyncio.create_task(run_job(job))
            if job.type == "once":
                await store.deactivate(job.id)
        await asyncio.sleep(30)
```

**Fast-parse** (portado do `cron_fast_parse.go`):
```python
# Cobre ~70% dos casos sem LLM
PATTERNS = [
    (r"todo dia (?:às?|as) (\d{1,2})h", "0 {h} * * *"),
    (r"toda (segunda|terça|quarta|quinta|sexta)", "0 9 * * {weekday}"),
    (r"daqui (\d+) min(?:uto)?s?", "once:{now + delta}"),
    (r"amanhã (?:às?|as) (\d{1,2})h", "once:{tomorrow at h}"),
    ...
]
```

---

### 3.7 Canal Telegram

**Fluxo de mensagem:**
```python
async def on_message(update: Update, context: BotContext) -> None:
    msg = update.message
    
    # 1. whitelist
    if not is_allowed(msg.from_user.id):
        return
    
    # 2. ack (reaction 👍)
    await ack(msg)
    
    # 3. command classifier (heurístico, sem LLM)
    cmd = match_command(msg.text)
    if cmd:
        await dispatch_command(cmd, msg)
        return
    
    # 4. intent + context + execute
    await dispatch_intent(msg)
```

**Comandos implementados (portados do Aurelia):**

| Comando | Handler |
|---|---|
| "nova conversa / reset" | `session_reset` |
| "agenda / me lembra" | `cron_create` |
| "meus agendamentos" | `cron_list` |
| "cancela agendamento" | `cron_cancel` |
| "status" | `status` |
| "quais agents" | `list_agents` |
| "/debug last" | `debug_last` |
| "/debug run <id>" | `debug_run` |
| "/debug errors" | `debug_errors` |
| "/projeto <nome>" | `project_bind` |

**Formatação Markdown → Telegram:**
- Converter `**bold**` → `*bold*` (MarkdownV2)
- Escapar caracteres especiais do Telegram
- Chunking para mensagens > 4096 chars

---

### 3.8 Canal HTTP API (chatapi)

```python
# FastAPI, porta :18790, só 127.0.0.1

@app.post("/api/chat")
async def chat(req: ChatRequest) -> ChatResponse:
    session = sessions.get_or_create(req.session_key)
    response = await executor.run(req.text, session)
    return ChatResponse(response=response, latency_ms=..., chat_id=session.id)

@app.post("/api/chat/stream")  # novo — SSE
async def chat_stream(req: ChatRequest) -> StreamingResponse:
    session = sessions.get_or_create(req.session_key)
    return StreamingResponse(executor.run_stream(req.text, session),
                             media_type="text/event-stream")

@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "model": model_manager.current}
```

---

### 3.9 Canal MCP Server

```python
# MCP server expondo Cláudio para Claude Code e outros clientes

@mcp.tool()
async def ask_claudio(prompt: str, context: str = "") -> str:
    """Envia um pedido ao Cláudio e retorna a resposta."""
    session = sessions.get_or_create("mcp-default")
    return await executor.run(prompt, session, profile="execute")

@mcp.tool()
async def run_agent(agent_id: str, input: str) -> str:
    """Delega uma tarefa diretamente a um agente AgentForge."""
    return await agentforge.run(agent_id, input)
```

---

### 3.10 Observabilidade

**RunLog (arquivo rotativo):**
```python
# ~/.claudio/logs/claudio.log
# Rotação diária, retenção 30 dias

logger.info("session.start", chat_id=chat_id, channel="telegram")
logger.info("tool.call", tool="run_bash", cmd="docker ps", duration_s=1.2)
logger.info("agent.start", agent="roteirista", input_tokens=312)
logger.warn("action.destructive", tool="run_bash", cmd="docker stop n8n", confirmed=True)
logger.error("tool.error", tool="http_get", url="...", error=str(e))
```

**Audit Log (agent_mesh, append-only):**
```python
# Tudo que modifica estado vai aqui também
async def audit(event: str, data: dict) -> None:
    await agent_mesh.insert_audit(
        agent="claudio",
        event=event,
        data=json.dumps(data),
    )
```

**Comandos de debug via Telegram (portados do Aurelia):**
- `/debug last` — última execução (status, duração, modelo, erro)
- `/debug run <id>` — timeline completa de um run
- `/debug errors` — últimos N runs com erro

---

## 4. Fluxo Completo — Exemplo de Execução

**Input:** "Cláudio, verifica quanto espaço tem no /mnt/vault"

```
1. Telegram on_message()
   → is_allowed(user_id) ✓
   → ack(reaction 👍)
   → match_command("verifica quanto espaço...") → None

2. Intent Classifier
   → heurística: contém "verifica" + path → IntentResult(
       type="execute",
       tools=["run_bash"],
       agent=None,
       context_hints=["filesystem", "vault"]
     )

3. Context Builder (~480 tokens)
   → IDENTITY_BLOCK (100t)
   → mem0.search("espaço vault filesystem") → 2 fragmentos (120t)
   → kuzu: Entity(vault) → rel: montado em /mnt/vault (40t)
   → intent_instructions: "use run_bash para consultas de sistema" (80t)
   → project_context: None

4. Model Manager
   → _current == "qwen3.5:27b" → nada a fazer

5. AgentForge Executor
   → profile="execute" → tools=[run_bash(read-only)]
   → 27b gera: run_bash("df -h /mnt/vault")
   → tool executa → "Filesystem: /dev/sdb1, Size: 930G, Used: 412G, Avail: 518G"
   → 27b formata resposta

6. Channel Output
   → Telegram send: "O /mnt/vault tem 518 GB livres de 930 GB (412 GB usados)."

7. Background (assíncrono, após resposta)
   → runlog: INFO tool.call run_bash df -h /mnt/vault 0.3s
   → audit_log: INSERT (agent=claudio, event=tool.call, data={tool, cmd, result_summary})
   → mem0 extraction: sem fatos novos relevantes → skip
```

---

## 5. Modelo de Dados — Sessão

```python
@dataclass
class Session:
    id: int                          # chat_id sintético
    channel: str                     # "telegram" | "http" | "mcp"
    chat_id: int                     # channel-specific ID
    thread_id: int | None            # Telegram thread
    project: str | None              # projeto ativo (/projeto <nome>)
    security_profile: str            # "chat" | "read" | "execute" | "privileged"
    history: list[Turn]              # turns da sessão atual
    created_at: datetime
    last_active: datetime

@dataclass
class Turn:
    role: str           # "user" | "assistant"
    content: str
    tool_calls: list    # tool calls deste turn
    timestamp: datetime
```

Sessões são in-memory. Não persistem entre restarts (histórico de sessão é volátil por design — a memória permanente vive no mem0).

---

## 6. Configuração

**`~/.claudio/config.json`:**
```json
{
    "telegram_bot_token": "...",
    "telegram_allowed_user_ids": [123456789],
    "ollama_url": "http://localhost:11434",
    "default_model": "qwen3.5:27b",
    "chatapi_port": 18790,
    "memory_dir": "~/.claudio/memory",
    "logs_dir": "~/.claudio/logs",
    "agent_mesh_db": "~/.agent-mesh/state.db",
    "security_profile_default": "execute"
}
```

**systemd user service (`~/.config/systemd/user/claudio.service`):**
```ini
[Unit]
Description=Cláudio v2 — AgentForge Assistant
After=network.target ollama.service

[Service]
Type=simple
WorkingDirectory=/home/conrado/repos/projetos/claudio-v2
ExecStart=python -m claudio.main
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

---

## 7. Decisões de Implementação

| Decisão | Escolha | Alternativa descartada | Razão |
|---|---|---|---|
| Linguagem | Python | Go (como Aurelia) | AgentForge já é Python; sem bridge |
| Telegram lib | `python-telegram-bot` | `telebot` (Go) | Ecossistema Python, async nativo |
| HTTP API | FastAPI | Flask | Async nativo, SSE built-in |
| Sessão | In-memory | SQLite | Memória permanente está no mem0; sessão é volátil |
| Cron persistence | SQLite dedicado | agent_mesh | Separação de responsabilidades |
| Embed model | `nomic-embed-text` via Ollama | OpenAI embeddings | On-prem first |

---

## 8. Ordem de Implementação

**Fase 1 — Core conversacional (MVP):**
1. `config.py` — carregamento e validação
2. `model_manager.py` — Ollama state tracker
3. `core/classifier.py` — heurística (portada do Aurelia)
4. `core/context.py` — identity + mem0 + stub project
5. `core/executor.py` — AgentForge wrapper
6. `channels/telegram.py` — bot básico (texto, whitelist, session reset)
7. `audit/runlog.py` + `audit/agent_mesh.py`

**Fase 2 — Memória e persistência:**
8. `memory/retrieval.py` — mem0 + kuzu integrados
9. `memory/extraction.py` — pós-sessão
10. `memory/migration.py` — import do Aurelia

**Fase 3 — Canais adicionais e cron:**
11. `channels/chatapi.py` — FastAPI :18790 + SSE
12. `cron/` — store + fast_parse + scheduler
13. `channels/mcp_server.py`

**Fase 4 — Observabilidade e polish:**
14. Comandos de debug (`/debug last`, `/debug run`, `/debug errors`)
15. Formatação Markdown → Telegram (chunking, escape)
16. `security/profiles.py` — enforcement por operação

---

*Este blueprint é o documento de implementação. A spec técnica (contratos de interface, modelo de dados completo, protocolos) é o próximo passo antes de codificar.*
