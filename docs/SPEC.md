# SPEC TÉCNICA — Cláudio v2

**Status:** Rascunho  
**Versão:** 0.1  
**Data:** 2026-06-14  
**Referências:** [PRD.md](./PRD.md) · [ARCHITECTURE.md](./ARCHITECTURE.md)

---

## 1. Interfaces dos Componentes

### 1.1 IntentClassifier

```python
from typing import Protocol
from dataclasses import dataclass, field

@dataclass(frozen=True)
class IntentResult:
    type: str                        # "chat" | "command" | "execute" | "research" | "delegate" | "cron"
    tools: list[str] = field(default_factory=list)   # tools a ativar (máx 3)
    agent: str | None = None         # sub-agente a delegar
    context_hints: list[str] = field(default_factory=list)  # hints para retrieval
    confidence: float = 1.0          # 1.0 = heurística, <1.0 = LLM fallback

class IntentClassifier(Protocol):
    async def classify(self, text: str, history: list["Turn"]) -> IntentResult: ...
```

**Regras de negócio:**
- `tools` nunca excede 3 itens — mais que isso degrada o qwen
- `confidence < 0.8` significa que a heurística não teve match forte; o LLM foi acionado
- `type = "command"` → dispatcher de comandos, não passa pelo executor LLM

---

### 1.2 ContextBuilder

```python
@dataclass(frozen=True)
class ContextBlock:
    content: str
    token_estimate: int
    source: str   # "identity" | "memory" | "kuzu" | "project" | "intent"

class ContextBuilder(Protocol):
    async def build(
        self,
        intent: IntentResult,
        session: "Session",
        max_tokens: int = 600,
    ) -> str: ...
    # Retorna system prompt pronto para injeção
    # Garante que token_estimate total <= max_tokens
    # Se ultrapassar: trunca memoria, depois kuzu, nunca identity
```

**Prioridade de truncamento (quando > 600 tokens):**
1. Kuzu (remove itens menos relevantes)
2. Memória (remove fragmentos de menor score)
3. Project context (trunca ao essencial)
4. Identity e intent instructions — nunca truncar

---

### 1.3 ModelManager

```python
class ModelManager(Protocol):
    @property
    def current(self) -> str | None: ...

    async def ensure(self, model: str) -> None:
        """Garante que `model` está na VRAM. Descarrega o modelo atual se diferente."""
        ...

    async def unload_all(self) -> None:
        """Descarrega qualquer modelo carregado. Usado no shutdown."""
        ...

    async def status(self) -> dict:
        """Retorna estado atual do Ollama: modelo carregado, VRAM usada."""
        ...
```

**Estados internos:**
```
UNKNOWN → (startup, consulta GET /api/ps) → IDLE | READY
IDLE → (ensure(model)) → LOADING → READY
READY(model_A) → (ensure(model_B)) → UNLOADING → LOADING → READY(model_B)
READY → (unload_all) → IDLE
```

**API Ollama usada:**
```
GET  /api/ps                          → lista modelos carregados
POST /api/generate {keep_alive: 0}   → descarrega modelo
POST /api/generate {keep_alive: "1h", prompt: ""} → pre-carrega (warmup)
```

---

### 1.4 Executor

```python
class Executor(Protocol):
    async def run(
        self,
        system_prompt: str,
        user_message: str,
        tools: list[str],
        session: "Session",
        security_profile: str,
    ) -> str: ...

    async def run_stream(
        self,
        system_prompt: str,
        user_message: str,
        tools: list[str],
        session: "Session",
        security_profile: str,
    ) -> AsyncIterator[str]: ...
```

**Contrato de segurança:**
- `tools` filtrado por `security_profile` antes de chegar ao AgentForge
- Ações destrutivas interceptadas antes da execução; se `security_profile != "privileged"` → erro
- Se `security_profile == "privileged"` → envia mensagem de confirmação ao usuário antes de executar

---

### 1.5 MemoryRetrieval

```python
@dataclass
class MemoryFragment:
    content: str
    score: float        # 0.0 – 1.0, relevância semântica
    source: str         # "mem0" | "kuzu"
    metadata: dict

class MemoryRetrieval(Protocol):
    async def search(
        self,
        query: str,
        context_hints: list[str],
        limit: int = 5,
        max_tokens: int = 300,
    ) -> list[MemoryFragment]: ...

    async def add(self, fact: str, metadata: dict = {}) -> None: ...
```

---

### 1.6 AuditLog

```python
class AuditLog(Protocol):
    async def log(
        self,
        event: str,           # ex: "tool.call", "session.start", "action.destructive"
        data: dict,           # payload do evento
        level: str = "info",  # "info" | "warn" | "error"
    ) -> None: ...
    # Escreve em AMBOS os destinos: runlog arquivo + agent_mesh audit_log
```

---

### 1.7 CronStore

```python
@dataclass
class CronJob:
    id: str              # UUID
    type: str            # "once" | "recurring"
    chat_id: int
    user_id: str
    prompt: str
    cron_expr: str | None   # apenas para "recurring"
    run_at: str | None      # ISO 8601, apenas para "once"
    active: bool
    created_at: str

class CronStore(Protocol):
    async def add_recurring(self, user_id: str, chat_id: int,
                             cron_expr: str, prompt: str) -> str: ...
    async def add_once(self, user_id: str, chat_id: int,
                        run_at: str, prompt: str) -> str: ...
    async def list_jobs(self, chat_id: int) -> list[CronJob]: ...
    async def delete_job(self, job_id: str) -> None: ...
    async def due_jobs(self, now: datetime) -> list[CronJob]: ...
    async def deactivate(self, job_id: str) -> None: ...
```

---

## 2. Modelos de Dados

### 2.1 Session

```python
@dataclass
class Session:
    id: int                          # chat_id sintético (auto-incremento)
    channel: str                     # "telegram" | "http" | "mcp"
    channel_id: str                  # ID nativo do canal (telegram chat_id, session_key HTTP)
    thread_id: int | None            # Telegram thread/tópico
    project: str | None              # projeto ativo
    security_profile: str            # "chat" | "read" | "execute" | "privileged"
    history: list["Turn"]            # turns in-memory, apenas sessão atual
    created_at: datetime
    last_active: datetime

    def to_prompt_history(self, max_turns: int = 20) -> list[dict]:
        """Converte os últimos N turns para formato de messages do Ollama."""
        ...
```

### 2.2 Turn

```python
@dataclass
class Turn:
    role: str                    # "user" | "assistant" | "tool"
    content: str
    tool_calls: list["ToolCall"]
    tool_results: list["ToolResult"]
    timestamp: datetime
    run_id: str                  # UUID do run para correlação no audit log
```

### 2.3 ToolCall / ToolResult

```python
@dataclass
class ToolCall:
    id: str           # UUID
    tool: str         # nome da tool
    args: dict        # argumentos

@dataclass
class ToolResult:
    call_id: str
    output: str
    error: str | None
    duration_ms: int
```

### 2.4 RunRecord (observabilidade)

```python
@dataclass
class RunRecord:
    run_id: str
    session_id: int
    channel: str
    chat_id: int
    thread_id: int | None
    user_id: str
    model: str
    status: str           # "running" | "completed" | "failed" | "timeout" | "canceled"
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    input_tokens: int
    output_tokens: int
    tool_calls_count: int
    error: str | None
    checkpoint: str | None   # último trecho da resposta (truncado, redactado)
    agent_name: str | None   # se delegado a sub-agente
```

---

## 3. Schemas de Banco de Dados

### 3.1 Cron (`~/.claudio/cron.db`)

```sql
CREATE TABLE jobs (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL CHECK(type IN ('once', 'recurring')),
    chat_id     INTEGER NOT NULL,
    user_id     TEXT NOT NULL,
    prompt      TEXT NOT NULL,
    cron_expr   TEXT,
    run_at      TEXT,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK(
        (type = 'recurring' AND cron_expr IS NOT NULL AND run_at IS NULL) OR
        (type = 'once'      AND run_at IS NOT NULL    AND cron_expr IS NULL)
    )
);

CREATE TABLE job_history (
    id          TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    job_id      TEXT NOT NULL REFERENCES jobs(id),
    ran_at      TEXT NOT NULL,
    status      TEXT NOT NULL CHECK(status IN ('ok', 'error')),
    error       TEXT,
    duration_ms INTEGER
);

CREATE INDEX idx_jobs_active_chat ON jobs(active, chat_id);
```

### 3.2 RunLog (`~/.claudio/runs.db`)

```sql
CREATE TABLE runs (
    run_id          TEXT PRIMARY KEY,
    session_id      INTEGER NOT NULL,
    channel         TEXT NOT NULL,
    chat_id         INTEGER NOT NULL,
    thread_id       INTEGER,
    user_id         TEXT NOT NULL,
    model           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'running',
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    duration_ms     INTEGER,
    input_tokens    INTEGER DEFAULT 0,
    output_tokens   INTEGER DEFAULT 0,
    tool_calls_count INTEGER DEFAULT 0,
    error           TEXT,
    checkpoint      TEXT,
    agent_name      TEXT
);

CREATE TABLE run_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL REFERENCES runs(run_id),
    phase       TEXT NOT NULL,     -- "classify" | "context" | "execute" | "tool" | "respond"
    level       TEXT NOT NULL DEFAULT 'info',
    message     TEXT,
    ts          INTEGER NOT NULL   -- unix timestamp ms
);

CREATE INDEX idx_runs_chat ON runs(chat_id, thread_id, started_at DESC);
CREATE INDEX idx_runs_status ON runs(status);
CREATE INDEX idx_events_run ON run_events(run_id);
```

### 3.3 Agent Mesh Audit Log (existente, `~/.agent-mesh/state.db`)

```sql
-- Tabela existente — apenas INSERT, nunca UPDATE/DELETE
-- Cláudio usa esta interface:
INSERT INTO audit_log (ts, agent, event, data)
VALUES (datetime('now'), 'claudio', ?, ?);
-- data: JSON com campos relevantes do evento
```

---

## 4. Contratos de API

### 4.1 HTTP API (`:18790`)

**`POST /api/chat`**
```
Request:
{
    "text": string,           // obrigatório
    "session_key": string,    // opcional, gerado automaticamente se ausente
    "images": [               // opcional
        {"data": "<base64>", "media_type": "image/jpeg"}
    ]
}

Response 200:
{
    "response": string,
    "latency_ms": integer,
    "chat_id": integer,
    "run_id": string
}

Response 400: {"error": "text is required"}
Response 408: {"error": "timeout"}
Response 500: {"error": "pipeline error: <msg>"}
```

**`POST /api/chat/stream`** (novo — SSE)
```
Request: mesmo formato de POST /api/chat

Response: text/event-stream
data: {"chunk": "pedaço da resposta"}
data: {"chunk": "..."}
data: {"done": true, "run_id": "...", "latency_ms": 1234}
```

**`GET /api/health`**
```
Response 200:
{
    "status": "ok",
    "model": "qwen3.5:27b",
    "model_loaded": true,
    "version": "2.0.0"
}
```

---

### 4.2 MCP Server

```python
# Ferramentas expostas:

@mcp.tool()
async def ask_claudio(
    prompt: str,
    context: str = "",          # contexto adicional opcional
    session_key: str = "mcp",   # permite multi-turn via MCP
) -> str:
    """Envia um pedido ao Cláudio e retorna a resposta completa."""

@mcp.tool()
async def run_agent(
    agent_id: str,              # ID do agente AgentForge
    input: str,                 # input para o agente
    timeout: int = 900,         # timeout em segundos
) -> str:
    """Delega uma tarefa diretamente a um agente AgentForge. Retorna o resultado."""

@mcp.tool()
async def claudio_status() -> dict:
    """Retorna o status atual do Cláudio (modelo, sessões ativas, runs recentes)."""
```

---

## 5. Protocolo de Memória

### 5.1 O que armazenar no mem0

**Armazena:**
- Decisões arquiteturais ("decidimos usar 27b-only para v2")
- Preferências do usuário ("Conrado prefere respostas diretas sem rodeios")
- Fatos técnicos relevantes ("fox-server tem 24GB VRAM, 2×RTX 3060")
- Resultados de benchmarks ("qwen3.5:27b: 25 tok/s com MTP")
- Projetos ativos e seu estado

**Não armazena:**
- Conteúdo literal de conversas
- Valores de variáveis temporários
- Resultados de comandos (logs, saídas de df/ps)
- Timestamps de quando algo foi dito

### 5.2 Formato dos fragmentos no mem0

```python
# Ao adicionar:
await mem0.add(
    messages=[{"role": "user", "content": fact_text}],
    user_id="conrado",
    metadata={
        "type": "fact" | "preference" | "decision" | "project",
        "project": project_name | None,
        "source": "extraction" | "explicit" | "migration",
    }
)

# Ao buscar:
results = await mem0.search(
    query=query_text,
    user_id="conrado",
    limit=5,
)
# results: [{"memory": "...", "score": 0.87, "metadata": {...}}]
```

### 5.3 Extração pós-sessão

**Trigger:** sessão encerrada (timeout 5min de inatividade ou reset explícito)

**Prompt de extração:**
```
Analise esta conversa e extraia fatos para memória permanente.

Regras:
- Apenas fatos novos, decisões ou preferências — não o que já é óbvio
- Um fato por linha, máximo 150 caracteres cada
- Formato: "<fato claro e autocontido>"
- Se não houver nada relevante, responda: NADA

Conversa:
{transcript}

Fatos:
```

**Pós-processamento:**
```python
def parse_facts(response: str) -> list[str]:
    if response.strip().upper() == "NADA":
        return []
    return [
        line.strip().lstrip("-•* ")
        for line in response.splitlines()
        if line.strip() and len(line.strip()) > 10
    ]
```

---

## 6. Protocolo de Segurança

### 6.1 Mapeamento tool → perfil mínimo

```python
TOOL_PROFILES: dict[str, str] = {
    # chat: apenas responde
    "send_claudio": "chat",

    # read: lê sem modificar
    "read_file": "read",
    "http_get": "read",
    "collect_system_health": "read",
    "run_bash_readonly": "read",   # cmds: df, ps, docker ps, cat, ls, ...

    # execute: modifica estado, mas não destrutivo
    "write_file": "execute",
    "append_file": "execute",
    "run_agent": "execute",
    "run_bash": "execute",         # cmds genéricos

    # privileged: destrutivo ou irreversível
    "run_bash_destructive": "privileged",  # rm, docker stop, systemctl stop, ...
}

DESTRUCTIVE_PATTERNS = [
    r"\brm\s+-rf?\b", r"\bdocker\s+stop\b", r"\bdocker\s+rm\b",
    r"\bsystemctl\s+stop\b", r"\bkill\b", r"\bpkill\b",
    r"\bdrop\s+table\b", r"\btruncate\b",
]
```

### 6.2 Fluxo de confirmação (ações destrutivas)

```
1. Executor detecta ação destrutiva no tool call gerado pelo 27b
2. Pausa execução
3. Envia para o canal: "⚠️ Ação destrutiva: `docker stop n8n`\nConfirma? (sim/não)"
4. Aguarda resposta do usuário (timeout: 60s)
5a. Usuário responde "sim" → executa + audit_log(confirmed=True)
5b. Usuário responde "não" ou timeout → cancela + audit_log(confirmed=False)
```

---

## 7. Protocolo de Erro

### 7.1 Hierarquia de exceções

```python
class ClaudioError(Exception):
    """Base para todos os erros do Cláudio."""

class ModelUnavailableError(ClaudioError):
    """Ollama não responde ou modelo não carrega."""

class ContextOverflowError(ClaudioError):
    """System prompt ultrapassou limite após truncamento."""

class SecurityViolationError(ClaudioError):
    """Tool call bloqueado por perfil de segurança."""

class ConfirmationTimeoutError(ClaudioError):
    """Usuário não confirmou ação destrutiva em 60s."""

class AgentDelegationError(ClaudioError):
    """Sub-agente retornou erro ou timeout."""

class MemoryError(ClaudioError):
    """Falha ao ler/escrever no mem0 ou Kuzu."""
```

### 7.2 Política de retry

| Erro | Retry | Backoff | Mensagem ao usuário |
|---|---|---|---|
| `ModelUnavailableError` | 3× | 5s exponencial | "Aguarda um momento, carregando o modelo..." |
| `AgentDelegationError` | 1× | imediato | "O agente falhou, tentando novamente..." |
| `MemoryError` | 0 | — | silencioso (log) — continua sem memória |
| `SecurityViolationError` | 0 | — | "Operação não permitida no perfil atual." |
| timeout Ollama (>900s) | 0 | — | "A execução demorou demais e foi cancelada." |

---

## 8. Configuração Completa

**`~/.claudio/config.json` — schema com defaults:**

```python
@dataclass
class Config:
    # Telegram
    telegram_bot_token: str               # obrigatório
    telegram_allowed_user_ids: list[int]  # obrigatório, mínimo 1
    telegram_allowed_group_ids: list[int] = field(default_factory=list)

    # Ollama
    ollama_url: str = "http://localhost:11434"
    default_model: str = "qwen3.5:27b"
    ollama_timeout_s: int = 900
    model_warmup_on_startup: bool = True

    # HTTP API
    chatapi_port: int = 18790
    chatapi_host: str = "127.0.0.1"

    # Memória
    mem0_collection: str = "claudio"
    embed_model: str = "nomic-embed-text"
    memory_extraction_min_turns: int = 3   # mínimo de turns para extrair

    # Caminhos
    config_dir: str = "~/.claudio"
    logs_dir: str = "~/.claudio/logs"
    cron_db: str = "~/.claudio/cron.db"
    runs_db: str = "~/.claudio/runs.db"
    agent_mesh_db: str = "~/.agent-mesh/state.db"

    # Segurança
    default_security_profile: str = "execute"

    # Logging
    log_retention_days: int = 30
    runlog_max_bytes: int = 50 * 1024 * 1024   # 50 MB por arquivo

    # AgentForge
    agentforge_path: str = "~/repos/estudo/agents-framework"
    agentforge_agents_dir: str = "~/repos/estudo/agents-framework/agents"

    @classmethod
    def load(cls) -> "Config":
        path = Path("~/.claudio/config.json").expanduser()
        if not path.exists():
            raise FileNotFoundError(f"Config não encontrada: {path}")
        data = json.loads(path.read_text())
        return cls(**data)

    def validate(self) -> None:
        assert self.telegram_bot_token, "telegram_bot_token é obrigatório"
        assert self.telegram_allowed_user_ids, "allowed_user_ids não pode ser vazio"
        assert self.default_security_profile in ("chat", "read", "execute", "privileged")
```

---

## 9. Variáveis de Ambiente

```bash
# Obrigatórias se não estiver no config.json
CLAUDIO_TELEGRAM_TOKEN=...
CLAUDIO_ALLOWED_USERS=123456789,987654321   # vírgula-separado

# Opcionais (override de config.json)
CLAUDIO_MODEL=qwen3.5:27b
CLAUDIO_OLLAMA_URL=http://localhost:11434
CLAUDIO_LOG_LEVEL=INFO    # DEBUG | INFO | WARNING | ERROR

# Herdadas do ambiente existente (não precisa configurar)
# OLLAMA_HOST já setado pelo sistema
```

---

## 10. Cron — Fast Parse (Spec de Padrões)

Portado de `cron_fast_parse.go`. Deve cobrir ≥ 70% dos casos sem LLM.

```python
# Formato de saída:
@dataclass
class FastParseResult:
    type: str         # "once" | "recurring"
    cron_expr: str | None
    run_at: datetime | None
    prompt: str       # ação a executar

FAST_PATTERNS: list[tuple[str, Callable]] = [
    # Diário
    (r"todo[s]? dia[s]? (?:às?|as) (\d{1,2})h(?:(\d{2})?)?",
     lambda m, now: FastParseResult("recurring", f"{m[2] or 0} {m[1]} * * *", None, "")),

    # Semanal
    (r"toda (segunda|terça|quarta|quinta|sexta|sábado|domingo)",
     lambda m, now: FastParseResult("recurring", f"0 9 * * {WEEKDAY[m[1]]}", None, "")),

    # Daqui N minutos
    (r"daqui (\d+) minutos?",
     lambda m, now: FastParseResult("once", None, now + timedelta(minutes=int(m[1])), "")),

    # Daqui N horas
    (r"daqui (\d+) horas?",
     lambda m, now: FastParseResult("once", None, now + timedelta(hours=int(m[1])), "")),

    # Hoje às Xh
    (r"hoje (?:às?|as) (\d{1,2})h",
     lambda m, now: FastParseResult("once", None, now.replace(hour=int(m[1]), minute=0), "")),

    # Amanhã às Xh
    (r"amanhã (?:às?|as) (\d{1,2})h",
     lambda m, now: FastParseResult("once", None, (now + timedelta(days=1)).replace(hour=int(m[1]), minute=0), "")),
]

WEEKDAY = {
    "segunda": 1, "terça": 2, "quarta": 3,
    "quinta": 4, "sexta": 5, "sábado": 6, "domingo": 0,
}
```

---

## 11. Lifecycle do Serviço

```
startup:
  1. Config.load() + validate()
  2. AuditLog.init() — abre connections (runlog file + agent_mesh db)
  3. ModelManager.probe() — GET /api/ps → descobre estado atual do Ollama
  4. MemoryMigration.run_if_needed() — verifica ~/.claudio/migration.done
  5. CronScheduler.start() — inicia loop asyncio (30s tick)
  6. Channels.start_all() — Telegram polling + FastAPI + MCP server
  7. ModelManager.ensure("qwen3.5:27b") se model_warmup_on_startup=True
  8. audit_log: INSERT (event="service.start")

shutdown (SIGTERM/SIGINT):
  1. audit_log: INSERT (event="service.stop")
  2. Channels.stop_all() — para polling e HTTP server (graceful, 30s)
  3. CronScheduler.stop()
  4. BackgroundWorker.drain() — aguarda extração de memória em andamento (max 120s)
  5. ModelManager.unload_all()
  6. AuditLog.close()
```

---

## 12. Checklist de Implementação por Fase

### Fase 1 — MVP conversacional
- [ ] `config.py` — `Config.load()`, `Config.validate()`
- [ ] `audit/runlog.py` — arquivo rotativo, `log(event, data, level)`
- [ ] `audit/agent_mesh.py` — INSERT `audit_log`
- [ ] `core/model_manager.py` — state machine, `ensure()`, `unload_all()`
- [ ] `core/classifier.py` — heurística portada + 27b fallback
- [ ] `core/context.py` — identity + stub mem0 (retorna vazio) + intent
- [ ] `core/executor.py` — AgentForge wrapper, sem tools por enquanto
- [ ] `channels/telegram.py` — bot, whitelist, session reset, texto básico
- [ ] `main.py` — DI wiring, lifecycle, systemd notify

**Critério de aceite Fase 1:** Cláudio responde via Telegram com qwen3.5:27b, system prompt < 600 tokens verificado, audit log gravado.

### Fase 2 — Memória
- [ ] `memory/retrieval.py` — mem0 + kuzu integrados
- [ ] `memory/extraction.py` — pós-sessão, prompt de extração, parse_facts
- [ ] `memory/migration.py` — import ~/.aurelia/memory/*.md
- [ ] `core/context.py` — mem0 retrieval real substituindo stub

**Critério de aceite Fase 2:** após 3 sessões, Cláudio menciona espontaneamente fato relevante de sessão anterior.

### Fase 3 — Canais adicionais e cron
- [ ] `channels/chatapi.py` — FastAPI :18790, POST /api/chat, SSE, /health
- [ ] `cron/store.py` — SQLite, add/list/delete/due_jobs
- [ ] `cron/fast_parse.py` — padrões portados, ≥ 70% cobertura
- [ ] `cron/scheduler.py` — loop 30s
- [ ] Telegram: comandos cron (create/list/cancel)
- [ ] `channels/mcp_server.py` — ask_claudio, run_agent

**Critério de aceite Fase 3:** n8n consegue falar com Cláudio via :18790; agendamento "todo dia às 9h" persiste e dispara.

### Fase 4 — Observabilidade e polish
- [ ] `runs.db` — RunRecord completo
- [ ] Telegram: `/debug last`, `/debug run <id>`, `/debug errors`
- [ ] `security/profiles.py` — enforcement + confirmação destrutiva
- [ ] Formatação Markdown → Telegram (chunking, escape MarkdownV2)
- [ ] Suporte a imagens (Telegram photo → base64 → 27b)
- [ ] STT via Groq (áudio → texto → pipeline normal)

**Critério de aceite Fase 4:** `/debug last` via Telegram mostra run_id, duração, modelo e status da última execução.

---

*A spec técnica é o contrato de implementação. Qualquer mudança de interface deve ser refletida aqui antes de ser codificada.*
