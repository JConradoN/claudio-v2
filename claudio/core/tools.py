from __future__ import annotations

import re
import subprocess

FETCH_SCRIPT = "/home/conrado/repos/producao/fox-vault/scripts/vault_ops/fetch_social.py"
FETCH_PYTHON = "/home/conrado/repos/producao/fox-vault/.venv/bin/python"

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
            "name": "fetch_social",
            "description": (
                "Busca e lê o conteúdo de uma URL (LinkedIn, artigos, posts). "
                "Usa browser com cookies autenticados. Retorna o texto extraído. "
                "Use quando o usuário pedir para ler, analisar ou resumir um link."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL completa a ser acessada.",
                    },
                    "analyze": {
                        "type": "boolean",
                        "description": "Se true, gera análise via LLM. Se false, retorna só o texto bruto.",
                        "default": True,
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
    }
]


def fetch_social(url: str, analyze: bool = True) -> str:
    """Busca URL via Scrapling+cookies e opcionalmente analisa com LLM."""
    cmd = [FETCH_PYTHON, FETCH_SCRIPT, url]
    if not analyze:
        cmd.append("--no-analyze")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        output = result.stdout or result.stderr or "(sem output)"
        if len(output) > 6000:
            output = output[:6000] + "\n... (truncado)"
        return output
    except subprocess.TimeoutExpired:
        return "[TIMEOUT] fetch_social demorou mais de 120s"
    except Exception as exc:
        return f"[ERRO] fetch_social: {exc}"


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
