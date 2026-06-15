from __future__ import annotations

import re

# Tools permitidas por perfil (acumulativo)
_PROFILE_TOOLS: dict[str, set[str]] = {
    "chat":       set(),
    "read":       {"read_file", "list_dir", "search_files"},
    "execute":    {"read_file", "list_dir", "search_files", "run_bash"},
    "privileged": {"read_file", "list_dir", "search_files", "run_bash", "write_file", "delete_file"},
}

# Padrões de comandos destrutivos que requerem confirmação mesmo em 'privileged'
_DESTRUCTIVE_PATTERNS = [
    re.compile(r"\brm\s+-rf?\b", re.I),
    re.compile(r"\bdrop\s+table\b", re.I),
    re.compile(r"\btruncate\b", re.I),
    re.compile(r"\bsystemctl\s+(stop|disable|mask)\b", re.I),
    re.compile(r"\bdocker\s+(rm|stop|kill)\b", re.I),
    re.compile(r"\bkill\s+-9\b"),
    re.compile(r"\bdd\s+if=", re.I),
    re.compile(r"\bmkfs\b", re.I),
    re.compile(r"\bshred\b", re.I),
    re.compile(r"\bformat\b", re.I),
]


def allowed_tools(security_profile: str, requested: list[str]) -> list[str]:
    """Filtra a lista de tools pelo perfil de segurança."""
    allowed = _PROFILE_TOOLS.get(security_profile, set())
    return [t for t in requested if t in allowed]


def is_destructive(command: str) -> bool:
    """Retorna True se o comando bate em algum padrão destrutivo."""
    return any(p.search(command) for p in _DESTRUCTIVE_PATTERNS)


def check_tool_allowed(tool_name: str, security_profile: str) -> bool:
    """Verifica se uma tool específica é permitida no perfil."""
    allowed = _PROFILE_TOOLS.get(security_profile, set())
    return tool_name in allowed
