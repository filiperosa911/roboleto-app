"""Normalização e validação de telefone brasileiro para o formato Z-API.

Formato final válido (celular): ``55`` + DDD(2) + ``9`` + 8 dígitos = 13 dígitos.

Reaproveita a lógica do projeto Aurex (``canonicalBrazilianPhone``): operadoras/
exports às vezes omitem o 9º dígito de celulares; reinserimos quando o local tem
8 dígitos e começa em 6/7/8/9. Fixos (8 dígitos começando 2–5) não funcionam no
WhatsApp e são rejeitados (caller pula e loga).
"""

from __future__ import annotations

import re

_NON_DIGITS = re.compile(r"\D")

# DDDs válidos no Brasil (Plano Nacional de Numeração).
VALID_DDDS = frozenset(
    {
        11, 12, 13, 14, 15, 16, 17, 18, 19,
        21, 22, 24, 27, 28,
        31, 32, 33, 34, 35, 37, 38,
        41, 42, 43, 44, 45, 46, 47, 48, 49,
        51, 53, 54, 55,
        61, 62, 63, 64, 65, 66, 67, 68, 69,
        71, 73, 74, 75, 77, 79,
        81, 82, 83, 84, 85, 86, 87, 88, 89,
        91, 92, 93, 94, 95, 96, 97, 98, 99,
    }
)


def only_digits(raw: str | None) -> str:
    return _NON_DIGITS.sub("", raw or "")


def canonical_brazilian_phone(raw: str | None) -> str | None:
    """Normaliza para ``55DDD9XXXXXXXX`` (13 dígitos) ou ``None`` se inválido.

    Inválido = vazio, DDD desconhecido, número fixo, ou comprimento que não
    converge para um celular brasileiro. O chamador deve pular e registrar.
    """
    digits = only_digits(raw)
    if not digits:
        return None

    # prefixo internacional de discagem (ex.: "0055...")
    if digits.startswith("00"):
        digits = digits[2:]
    # prefixo de tronco interurbano "0" (ex.: 011 99999-9999) -> remove o 0
    elif digits.startswith("0") and len(digits) in (11, 12):
        digits = digits[1:]

    # garante código do país 55
    if len(digits) in (10, 11) and not digits.startswith("55"):
        # número local (DDD + assinante) sem código de país
        digits = "55" + digits
    elif digits.startswith("55") and len(digits) >= 12:
        pass  # já tem código de país
    elif len(digits) in (10, 11) and digits.startswith("55"):
        # 10/11 dígitos começando em 55 = DDD 55 (RS) sem código de país
        digits = "55" + digits

    if len(digits) < 12:
        return None

    ddd = digits[2:4]
    local = digits[4:]

    if not ddd.isdigit() or int(ddd) not in VALID_DDDS:
        return None

    # reinsere o 9º dígito de celular se foi omitido
    if len(local) == 8 and local[0] in "6789":
        local = "9" + local

    # fixo (8 dígitos começando 2–5) — não serve para WhatsApp
    if len(local) == 8 and local[0] in "2345":
        return None

    # celular válido: 9 dígitos começando em 9
    if len(local) != 9 or local[0] != "9":
        return None

    return "55" + ddd + local


def is_valid_whatsapp(raw: str | None) -> bool:
    return canonical_brazilian_phone(raw) is not None


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_plausible_email(raw: str | None) -> bool:
    """Validação sintática leve de e-mail (não verifica entregabilidade)."""
    return bool(raw and _EMAIL_RE.match(raw.strip()))


__all__ = [
    "VALID_DDDS",
    "only_digits",
    "canonical_brazilian_phone",
    "is_valid_whatsapp",
    "is_plausible_email",
]
