from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

WEEKDAY = {
    "segunda": 1, "terça": 2, "terca": 2,
    "quarta": 3, "quinta": 4, "sexta": 5,
    "sábado": 6, "sabado": 6, "domingo": 0,
}


@dataclass
class FastParseResult:
    type: str               # "once" | "recurring"
    cron_expr: str | None
    run_at: datetime | None
    prompt: str


def _hm(h: str, m: str | None) -> tuple[int, int]:
    return int(h), int(m) if m else 0


# (padrão, factory) — avaliados em ordem, retorna no primeiro match
_PATTERNS: list[tuple[re.Pattern, object]] = [
    # todo[s] dia[s] às Xh[Mm]
    (
        re.compile(r"todo[s]?\s+dia[s]?\s+(?:às?|as)\s+(\d{1,2})h(?:(\d{2})?)?", re.I),
        lambda m, now: FastParseResult(
            "recurring",
            f"{_hm(m.group(1), m.group(2))[1]} {_hm(m.group(1), m.group(2))[0]} * * *",
            None, "",
        ),
    ),
    # toda segunda|terça|... [às Xh]
    (
        re.compile(
            r"toda\s+(segunda|terça|terca|quarta|quinta|sexta|sábado|sabado|domingo)"
            r"(?:\s+(?:às?|as)\s+(\d{1,2})h(?:(\d{2})?)?)?",
            re.I,
        ),
        lambda m, now: FastParseResult(
            "recurring",
            f"{_hm(m.group(2) or '9', m.group(3))[1]} "
            f"{_hm(m.group(2) or '9', m.group(3))[0]} "
            f"* * {WEEKDAY[m.group(1).lower()]}",
            None, "",
        ),
    ),
    # toda hora | a cada hora
    (
        re.compile(r"tod[ao]\s+hora|a\s+cada\s+hora", re.I),
        lambda m, now: FastParseResult("recurring", "0 * * * *", None, ""),
    ),
    # a cada N minutos
    (
        re.compile(r"a\s+cada\s+(\d+)\s+minutos?", re.I),
        lambda m, now: FastParseResult("recurring", f"*/{m.group(1)} * * * *", None, ""),
    ),
    # a cada N horas
    (
        re.compile(r"a\s+cada\s+(\d+)\s+horas?", re.I),
        lambda m, now: FastParseResult("recurring", f"0 */{m.group(1)} * * *", None, ""),
    ),
    # daqui N minutos
    (
        re.compile(r"daqui\s+(\d+)\s+minutos?", re.I),
        lambda m, now: FastParseResult(
            "once", None, now + timedelta(minutes=int(m.group(1))), ""
        ),
    ),
    # daqui N horas
    (
        re.compile(r"daqui\s+(\d+)\s+horas?", re.I),
        lambda m, now: FastParseResult(
            "once", None, now + timedelta(hours=int(m.group(1))), ""
        ),
    ),
    # hoje às Xh[Mm]
    (
        re.compile(r"hoje\s+(?:às?|as)\s+(\d{1,2})h(?:(\d{2})?)?", re.I),
        lambda m, now: FastParseResult(
            "once", None,
            now.replace(hour=int(m.group(1)), minute=int(m.group(2) or 0), second=0, microsecond=0),
            "",
        ),
    ),
    # amanhã às Xh[Mm]
    (
        re.compile(r"amanhã\s+(?:às?|as)\s+(\d{1,2})h(?:(\d{2})?)?", re.I),
        lambda m, now: FastParseResult(
            "once", None,
            (now + timedelta(days=1)).replace(
                hour=int(m.group(1)), minute=int(m.group(2) or 0), second=0, microsecond=0
            ),
            "",
        ),
    ),
    # todo[s] dia[s] (sem hora → 9h)
    (
        re.compile(r"todo[s]?\s+dia[s]?", re.I),
        lambda m, now: FastParseResult("recurring", "0 9 * * *", None, ""),
    ),
    # toda semana [às Xh]
    (
        re.compile(r"toda\s+semana(?:\s+(?:às?|as)\s+(\d{1,2})h(?:(\d{2})?)?)?", re.I),
        lambda m, now: FastParseResult(
            "recurring",
            f"{_hm(m.group(1) or '9', m.group(2))[1]} "
            f"{_hm(m.group(1) or '9', m.group(2))[0]} * * 1",
            None, "",
        ),
    ),
    # todo mês dia N
    (
        re.compile(r"todo\s+mês?\s+dia\s+(\d{1,2})", re.I),
        lambda m, now: FastParseResult(
            "recurring", f"0 9 {m.group(1)} * *", None, ""
        ),
    ),
]


def _extract_prompt(text: str, match: re.Match) -> str:
    """Remove a parte temporal do texto, retornando a ação."""
    remaining = text[:match.start()].strip() + " " + text[match.end():].strip()
    # Remove conectivos comuns
    remaining = re.sub(r"^\s*(?:me avise|me lembre|me lembrar|lembre-me|lembrar|avise-me|:)\s*", "", remaining.strip(), flags=re.I)
    return remaining.strip()


def fast_parse(text: str, now: datetime | None = None) -> FastParseResult | None:
    """
    Tenta extrair expressão cron/datetime de texto em PT-BR.
    Retorna None se nenhum padrão coincidir (deve cair para LLM).
    """
    if now is None:
        now = datetime.now()

    for pattern, factory in _PATTERNS:
        m = pattern.search(text)
        if m:
            result: FastParseResult = factory(m, now)  # type: ignore[operator]
            result.prompt = _extract_prompt(text, m) or text
            return result

    return None
