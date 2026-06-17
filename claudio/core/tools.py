from __future__ import annotations

import re
import subprocess

_AGENTFORGE_ROOT = "/home/conrado/repos/estudo/agents-framework"
_AGENTFORGE_PYTHON = "/home/conrado/repos/estudo/agents-framework/.venv/bin/python"
_LINK_READER_DIR = f"{_AGENTFORGE_ROOT}/agents/link-reader"

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
    }
]


def read_link(url: str) -> str:
    """Delega leitura de URL ao agente link-reader do AgentForge."""
    import json as _json
    cmd = [
        _AGENTFORGE_PYTHON, "-m", "agentforge.cli.main", "run",
        "--agent-dir", _LINK_READER_DIR,
        "--input", url,
        "--mode", "raw",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300,
            cwd=_AGENTFORGE_ROOT,
            env={**__import__("os").environ, "PYTHONPATH": f"{_AGENTFORGE_ROOT}/src"},
        )
        raw = result.stdout.strip()
        if not raw:
            return result.stderr or "(sem output do agente)"
        try:
            data = _json.loads(raw)
            output = data.get("output", raw)
        except _json.JSONDecodeError:
            output = raw
        if len(output) > 6000:
            output = output[:6000] + "\n... (truncado)"
        return output
    except subprocess.TimeoutExpired:
        return "[TIMEOUT] link-reader demorou mais de 180s"
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
