"""Configuração de logging: console + arquivo rotativo diário.

Inclui helpers de máscara de PII (CPF/telefone/e-mail) para cumprir LGPD — nunca
logamos o dado completo em nível INFO.
"""

from __future__ import annotations

import logging
import re
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

_DIGITS = re.compile(r"\D")


def mask_cpf(cpf: str | None) -> str:
    """``12345678909`` -> ``***.***.789-09`` (mostra só os 5 últimos dígitos)."""
    if not cpf:
        return "?"
    digits = _DIGITS.sub("", cpf)
    if len(digits) < 5:
        return "***"
    return f"***.***.{digits[-5:-2]}-{digits[-2:]}"


def mask_phone(phone: str | None) -> str:
    """Mostra só os 4 últimos dígitos do telefone."""
    if not phone:
        return "?"
    digits = _DIGITS.sub("", phone)
    if len(digits) < 4:
        return "***"
    return f"***{digits[-4:]}"


def mask_email(email: str | None) -> str:
    """``maria.silva@gmail.com`` -> ``m***@gmail.com``."""
    if not email or "@" not in email:
        return "?"
    local, _, domain = email.partition("@")
    head = local[0] if local else "?"
    return f"{head}***@{domain}"


def setup_logging(log_dir: Path, level: str = "INFO") -> logging.Logger:
    """Configura o logger raiz e devolve o logger do app.

    - Console: ``HH:MM:SS LEVEL mensagem``
    - Arquivo: ``logs/regua-YYYY-MM-DD.log`` rotacionado à meia-noite (30 dias).
    """
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    # evita handlers duplicados se chamado mais de uma vez
    for h in list(root.handlers):
        root.removeHandler(h)

    console = logging.StreamHandler()
    console.setLevel(getattr(logging, level.upper(), logging.INFO))
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%H:%M:%S"))
    root.addHandler(console)

    file_handler = TimedRotatingFileHandler(
        log_dir / "regua.log", when="midnight", backupCount=30, encoding="utf-8"
    )
    file_handler.suffix = "%Y-%m-%d"
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-5s [%(name)s] %(message)s")
    )
    root.addHandler(file_handler)

    return logging.getLogger("seguros")


__all__ = ["setup_logging", "mask_cpf", "mask_phone", "mask_email"]
