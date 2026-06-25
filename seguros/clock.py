"""Utilitários de tempo e timezone.

Todo datetime de *estado* é armazenado como ISO-8601 UTC com sufixo ``Z``.
As decisões de *janela de horário* usam o timezone local (America/Sao_Paulo).

Centralizar isto aqui dá um único ponto de injeção para testes (fake clock) e
garante que nunca se use ``datetime.now()`` ingênuo (sujeito a drift do SO).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

# --- relógio (injetável em testes) -------------------------------------------

_now_override: datetime | None = None


def set_now_override(dt: datetime | None) -> None:
    """Fixa o "agora" para testes. Passe ``None`` para voltar ao relógio real."""
    global _now_override
    if dt is not None and dt.tzinfo is None:
        raise ValueError("override de agora deve ser timezone-aware")
    _now_override = dt


def now_utc() -> datetime:
    """Agora em UTC (timezone-aware)."""
    if _now_override is not None:
        return _now_override.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def now_in(tz_name: str) -> datetime:
    """Agora no timezone informado (ex.: ``America/Sao_Paulo``)."""
    return now_utc().astimezone(ZoneInfo(tz_name))


# --- serialização ISO --------------------------------------------------------


def iso_utc(dt: datetime | None = None) -> str:
    """Serializa um datetime para ISO-8601 UTC com ``Z`` (default: agora)."""
    dt = dt or now_utc()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str | None) -> datetime | None:
    """Lê um ISO-8601 (com ``Z`` ou offset) de volta para datetime UTC-aware."""
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# --- janela de horário / cadência --------------------------------------------


def within_send_window(
    now_local: datetime,
    start: time,
    end: time,
    weekdays_only: bool,
) -> bool:
    """True se ``now_local`` está dentro da janela de envio e (se exigido) em dia útil."""
    if weekdays_only and now_local.weekday() >= 5:  # 5=sáb, 6=dom
        return False
    return start <= now_local.timetz().replace(tzinfo=None) <= end


def days_since(enrolled_iso: str, reference: datetime | None = None) -> int:
    """Número de dias (calendário UTC) desde ``enrolled_iso`` até a referência."""
    enrolled = parse_iso(enrolled_iso)
    if enrolled is None:
        return 0
    ref = reference or now_utc()
    return (ref.date() - enrolled.date()).days


def parse_hhmm(value: str) -> time:
    """Lê uma string ``HH:MM`` em :class:`datetime.time`."""
    hh, mm = value.strip().split(":", 1)
    return time(int(hh), int(mm))


__all__ = [
    "set_now_override",
    "now_utc",
    "now_in",
    "iso_utc",
    "parse_iso",
    "within_send_window",
    "days_since",
    "parse_hhmm",
    "date",
    "timedelta",
]
