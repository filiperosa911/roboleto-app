"""E-mail via Gmail SMTP (STARTTLS) — senders live e dry-run.

Constrói ``multipart/alternative`` (texto + HTML leve). Erro de autenticação é
fatal (App Password errada / 2FA desligado) e propaga para abortar o lote de
e-mail; erros por destinatário viram ``SendResult`` e não derrubam o run.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from ..domain.models import Resultado
from .phone import is_plausible_email
from .whatsapp import SendResult

log = logging.getLogger("seguros.email")


class EmailError(Exception):
    """Erro genérico de envio de e-mail."""


class SmtpAuthError(EmailError):
    """Falha de autenticação SMTP — App Password inválida ou 2FA desativado."""


def _build_message(
    *, from_name: str, from_addr: str, to_addr: str, subject: str, text: str, html: str | None
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = f"{from_name} <{from_addr}>" if from_name else from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Reply-To"] = from_addr
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


class SmtpSender:
    """Sender de e-mail ao vivo via Gmail SMTP."""

    def __init__(
        self,
        *,
        host: str = "smtp.gmail.com",
        port: int = 587,
        user: str,
        password: str,
        from_name: str = "",
        timeout: int = 30,
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.from_name = from_name
        self.timeout = timeout

    def send(self, to_addr: str, subject: str, text: str, html: str | None = None) -> SendResult:
        if not is_plausible_email(to_addr):
            return SendResult(False, Resultado.EMAIL_INVALIDO, detail=f"inválido: {to_addr!r}")
        msg = _build_message(
            from_name=self.from_name,
            from_addr=self.user,
            to_addr=to_addr,
            subject=subject,
            text=text,
            html=html,
        )
        try:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                smtp.login(self.user, self.password)
                smtp.send_message(msg)
        except smtplib.SMTPAuthenticationError as err:
            raise SmtpAuthError(
                "autenticação SMTP falhou — confira GMAIL_APP_PASSWORD (App Password de 16 "
                "caracteres com 2FA ativo). Veja o README."
            ) from err
        except smtplib.SMTPRecipientsRefused as err:
            return SendResult(False, Resultado.ERRO, detail=f"destinatário recusado: {err}")
        except (smtplib.SMTPException, OSError) as err:
            return SendResult(False, Resultado.ERRO, detail=str(err))
        return SendResult(True, Resultado.ENVIADO)


class DryRunEmail:
    """Sender de e-mail em dry-run: não envia, devolve ``DRY_RUN``."""

    def send(self, to_addr: str, subject: str, text: str, html: str | None = None) -> SendResult:
        if not is_plausible_email(to_addr):
            return SendResult(False, Resultado.EMAIL_INVALIDO, detail=f"inválido: {to_addr!r}")
        return SendResult(False, Resultado.DRY_RUN, detail="dry-run: não enviado")


__all__ = ["SmtpSender", "DryRunEmail", "EmailError", "SmtpAuthError"]
