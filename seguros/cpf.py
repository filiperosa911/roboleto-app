"""Normalização de CPF — o ponto de junção entre as telas da MAG.

Sempre comparamos/armazenamos a forma de 11 dígitos (``zfill`` cobre CPFs com
zero à esquerda que a UI às vezes trunca).
"""

from __future__ import annotations

import re

_NON_DIGITS = re.compile(r"\D")


def normalize_cpf(raw: str | None) -> str:
    digits = _NON_DIGITS.sub("", raw or "")
    return digits.zfill(11) if digits else ""


def format_cpf(digits: str) -> str:
    d = normalize_cpf(digits)
    if len(d) != 11:
        return d
    return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"


__all__ = ["normalize_cpf", "format_cpf"]
