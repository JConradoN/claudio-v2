from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

_FETCH_SCRIPT = "/home/conrado/repos/producao/fox-vault/scripts/vault_ops/fetch_social.py"
_FETCH_PYTHON = "/home/conrado/repos/producao/fox-vault/.venv/bin/python"

_AGENTFORGE_DIR = "/home/conrado/repos/estudo/agents-framework"
_AGENTFORGE_PYTHON = "/home/conrado/.local/share/claudio-venv/bin/python"

_MD_STRIP = re.compile(
    r"(?:```[\s\S]*?```"           # fenced code blocks
    r"|`[^`]+`"                    # inline code
    r"|\*\*(.+?)\*\*"              # bold **
    r"|\*(.+?)\*"                  # italic *
    r"|^#{1,6}\s+"                 # headings
    r"|^\s*[-*+]\s+"               # bullet lists
    r"|\\([_\[\]()~`>#+\-=|{}.!]))" # MarkdownV2 escapes
    r"",
    re.MULTILINE,
)


def _strip_markdown(text: str) -> str:
    """Remove markdown e MarkdownV2 escapes para devolver texto limpo ao LLM."""
    text = re.sub(r"```[\s\S]*?```", lambda m: m.group(0).replace("```", ""), text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\\([_\[\]()~`>#+\-=|{}.!\\])", r"\1", text)
    return text.strip()

# Comandos permitidos no perfil read-only
_READONLY_ALLOWLIST = re.compile(
    r"^\s*(df|free|ps|top|uptime|cat|ls|tail|head|grep|docker\s+ps|docker\s+stats|"
    r"nvidia-smi|systemctl\s+status|journalctl|uname|hostname|ip\s+addr|"
    r"lscpu|lsblk|du\s+-[shH]|ping|curl\s+-[sI]|wget\s+--spider|"
    r"sqlite3\s.+SELECT)\b"
)

_DESTRUCTIVE = re.compile(
    r"\b(rm|rmdir|mv|dd|mkfs|fdisk|parted|shred|truncate|"
    r"docker\s+(stop|rm|kill|rmi|down)|systemctl\s+(stop|disable|mask)|"
    r"kill|pkill|killall|reboot|shutdown|poweroff|halt|"
    r"DROP|DELETE|UPDATE|INSERT|ALTER)\b",
    re.IGNORECASE,
)

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": (
                "Salva um fato ou aprendizado na memória persistente do Cláudio (mem0 + agent-mesh). "
                "Use quando o usuário pedir para lembrar algo, ou quando você aprender uma preferência, "
                "regra de formatação, configuração ou qualquer informação que deve persistir entre sessões. "
                "Exemplos: preferências de formatação, decisões arquiteturais, regras de interação."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "fact": {
                        "type": "string",
                        "description": "O fato ou regra a ser memorizado. Seja específico e completo.",
                    },
                    "key": {
                        "type": "string",
                        "description": "Chave identificadora no agent-mesh (ex: 'claudio:formato_telegram'). Use prefixo 'claudio:'.",
                    },
                },
                "required": ["fact"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_link",
            "description": (
                "Lê e analisa uma URL (LinkedIn, artigos, posts, qualquer página web). "
                "Usa browser autenticado via agente especializado. "
                "Use sempre que o usuário enviar um link ou pedir para ler/analisar uma URL."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL completa a ser acessada.",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": (
                "Executa um comando bash no fox-server e retorna a saída. "
                "Use para verificar status do sistema, disco, memória, GPU, containers, logs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Comando bash a executar. Prefira comandos de leitura.",
                    }
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_agents",
            "description": (
                "Lista todos os agentes disponíveis no AgentForge com nome, ID e propósito. "
                "Use quando o usuário perguntar quais agentes existem ou o que cada um faz."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_agent",
            "description": (
                "Aciona um agente do AgentForge com uma tarefa específica e retorna o resultado. "
                "Use quando o usuário pedir para executar uma tarefa delegável a um agente especializado. "
                "Se não souber o agent_id exato, chame list_agents primeiro para descobrir os disponíveis."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": (
                            "ID do agente a acionar (nome do diretório em agents/). "
                            "Use list_agents para ver todos os IDs disponíveis."
                        ),
                    },
                    "input": {
                        "type": "string",
                        "description": "Tarefa ou pergunta a ser processada pelo agente.",
                    },
                },
                "required": ["agent_id", "input"],
            },
        },
    },
]


def save_memory(fact: str, key: str | None = None) -> str:
    """Salva fato no mem0 e no agent-mesh."""
    results = []

    # 1. mem0 via subprocess para evitar import pesado
    try:
        script = (
            "import sys; sys.path.insert(0, '/home/conrado/repos/projetos/claudio-v2'); "
            "from claudio.config import Config; from claudio.memory.retrieval import MemoryManager; "
            "import asyncio; "
            f"c=Config.load(); m=MemoryManager(c); asyncio.run(m.add([{fact!r}], {{'source_type':'explicit'}}))"
        )
        r = subprocess.run(
            [_AGENTFORGE_PYTHON, "-c", script],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode == 0:
            results.append("mem0: gravado")
        else:
            results.append(f"mem0: erro — {r.stderr[-200:]}")
    except Exception as e:
        results.append(f"mem0: {e}")

    # 2. agent-mesh
    if key:
        try:
            import json as _json
            payload = _json.dumps({"fact": fact})
            r2 = subprocess.run(
                ["python3", "/home/conrado/.agent-mesh/write-memory.py", key, payload, "claudio"],
                capture_output=True, text=True, timeout=30,
            )
            results.append("agent-mesh: gravado" if r2.returncode == 0 else f"agent-mesh: {r2.stderr[-100:]}")
        except Exception as e:
            results.append(f"agent-mesh: {e}")

    return " | ".join(results) or "gravado"


def read_link(url: str) -> str:
    """Busca URL via Scrapling+cookies e retorna texto bruto para análise pelo Cláudio."""
    cmd = [_FETCH_PYTHON, _FETCH_SCRIPT, url, "--no-analyze"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        output = result.stdout or result.stderr or "(sem output)"
        if len(output) > 6000:
            output = output[:6000] + "\n... (truncado)"
        return output
    except subprocess.TimeoutExpired:
        return "[TIMEOUT] read_link: browser demorou mais de 120s"
    except Exception as exc:
        return f"[ERRO] read_link: {exc}"


def run_bash(command: str, security_profile: str = "execute") -> str:
    """Executa comando com restrições por perfil de segurança."""
    if _DESTRUCTIVE.search(command):
        if security_profile != "privileged":
            return f"[BLOQUEADO] Comando destrutivo requer confirmação: `{command}`"

    if security_profile == "read" and not _READONLY_ALLOWLIST.match(command):
        return f"[BLOQUEADO] Comando não permitido no perfil read-only: `{command}`"

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = result.stdout + result.stderr
        if len(output) > 3000:
            output = output[:3000] + "\n... (truncado)"
        return output or "(sem saída)"
    except subprocess.TimeoutExpired:
        return "[TIMEOUT] Comando demorou mais de 30s"
    except Exception as exc:
        return f"[ERRO] {exc}"


def list_agents() -> str:
    """Lista agentes disponíveis no AgentForge lendo os agent.yaml de cada diretório."""
    agents_root = Path(_AGENTFORGE_DIR) / "agents"
    if not agents_root.exists():
        return f"[ERRO] Diretório de agentes não encontrado: {agents_root}"

    lines = ["Agentes disponíveis no AgentForge:\n"]
    skip = {"mock_agent", "claudio"}  # agentes internos/de teste

    for agent_dir in sorted(agents_root.iterdir()):
        if not agent_dir.is_dir() or agent_dir.name in skip:
            continue
        yaml_path = agent_dir / "agent.yaml"
        if not yaml_path.exists():
            continue
        try:
            content = yaml_path.read_text()
            agent_id = _extract_yaml_field(content, "id") or agent_dir.name
            name = _extract_yaml_field(content, "name") or agent_id
            purpose = _extract_yaml_field(content, "purpose") or "sem descrição"
            lines.append(f"- {agent_id} ({name}): {purpose}")
        except Exception:
            lines.append(f"- {agent_dir.name}: (erro ao ler agent.yaml)")

    return "\n".join(lines)


def _extract_yaml_field(content: str, field: str) -> str:
    """Extrai campo simples de YAML sem dependência externa."""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{field}:"):
            value = stripped[len(field) + 1:].strip().strip("'\"")
            if value and not value.startswith("|") and not value.startswith(">"):
                return value
    return ""


def run_agent(agent_id: str, input_text: str) -> str:
    """Aciona um agente AgentForge via CLI e retorna o resultado."""
    agents_root = Path(_AGENTFORGE_DIR) / "agents"
    agent_dir = agents_root / agent_id

    if not agent_dir.exists():
        available = [d.name for d in agents_root.iterdir() if d.is_dir() and d.name != "mock_agent"]
        return f"[ERRO] Agente '{agent_id}' não encontrado. Disponíveis: {', '.join(sorted(available))}"

    env = {
        **os.environ,
        "PYTHONPATH": f"{_AGENTFORGE_DIR}/src",
        "AGENTFORGE_PROVIDER": "llamacpp",
        "LLAMACPP_BASE_URL": "http://localhost:8082",
        "LLAMACPP_THINKING_BUDGET": "0",
    }

    try:
        result = subprocess.run(
            [
                _AGENTFORGE_PYTHON, "-m", "agentforge.cli.main",
                "run",
                "--agent-dir", str(agent_dir),
                "--input", input_text,
                "--mode", "pretty",
            ],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=_AGENTFORGE_DIR,
            env=env,
        )
        output = result.stdout or result.stderr or "(sem saída)"
        output = _strip_markdown(output)
        if len(output) > 4000:
            output = output[:4000] + "\n... (truncado)"
        return output
    except subprocess.TimeoutExpired:
        return f"[TIMEOUT] Agente '{agent_id}' demorou mais de 300s"
    except Exception as exc:
        return f"[ERRO] run_agent: {exc}"
