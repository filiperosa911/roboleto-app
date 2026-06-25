"""Z-API (WhatsApp) — cliente HTTP + senders (live e dry-run).

Reaproveita o padrão comprovado do projeto Aurex:
- base ``https://api.z-api.io/instances/{id}/token/{token}``
- header ``Client-Token`` + ``Content-Type: application/json``
- retry do erro "smartphone is not responding" (400) via ``GET /restart`` + esperas
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass

import requests

from ..domain.models import Resultado
from .phone import canonical_brazilian_phone

log = logging.getLogger("seguros.whatsapp")

_RESTART_BACKOFF_S = (12, 20)  # esperas entre tentativas após "smartphone offline"


class ZApiError(Exception):
    """Erro base do Z-API."""


class ZApiHTTPError(ZApiError):
    def __init__(self, status: int, endpoint: str, body) -> None:
        super().__init__(f"Z-API {endpoint} falhou: HTTP {status}")
        self.status = status
        self.endpoint = endpoint
        self.body = body


class ZApiSmartphoneOffline(ZApiError):
    """O celular pareado parou de responder (sessão estagnada)."""


class ZApiNotConnected(ZApiError):
    """Instância desconectada / precisa de QR code."""


@dataclass
class SendResult:
    """Resultado de uma tentativa de envio (WhatsApp ou e-mail)."""

    sent: bool  # True só se realmente enviou (live, sucesso)
    resultado: Resultado
    message_id: str | None = None
    detail: str | None = None


def _is_smartphone_offline(body) -> bool:
    if not isinstance(body, dict):
        return False
    msg = body.get("error")
    return isinstance(msg, str) and "smartphone is not responding" in msg.lower()


class ZApiClient:
    """Wrapper HTTP de baixo nível sobre a API do Z-API."""

    def __init__(
        self,
        instance_id: str,
        token: str,
        client_token: str,
        *,
        timeout: int = 30,
        session: requests.Session | None = None,
    ) -> None:
        self._base = f"https://api.z-api.io/instances/{instance_id}/token/{token}"
        self._headers = {"Content-Type": "application/json"}
        # Client-Token só é enviado se a conta tiver o token de segurança ativado.
        if client_token:
            self._headers["Client-Token"] = client_token
        self._timeout = timeout
        self._session = session or requests.Session()

    def _call(self, endpoint: str, *, method: str = "POST", body: dict | None = None) -> dict:
        url = f"{self._base}{endpoint}"
        resp = self._session.request(
            method, url, json=body, headers=self._headers, timeout=self._timeout
        )
        try:
            parsed = resp.json() if resp.content else {}
        except ValueError:
            parsed = {"raw": resp.text}
        if not resp.ok:
            raise ZApiHTTPError(resp.status_code, endpoint, parsed)
        return parsed

    def get_status(self) -> dict:
        return self._call("/status", method="GET")

    def restart(self) -> dict:
        return self._call("/restart", method="GET")

    def send_text(
        self,
        phone: str,
        message: str,
        *,
        delay_message: int | None = None,
        delay_typing: int | None = None,
    ) -> dict:
        """POST /send-text com retry para "smartphone offline"."""
        body: dict = {"phone": phone, "message": message}
        if delay_message:
            body["delayMessage"] = max(1, min(15, delay_message))
        if delay_typing:
            body["delayTyping"] = max(1, min(15, delay_typing))

        last_err: ZApiError | None = None
        for attempt in range(len(_RESTART_BACKOFF_S) + 1):
            try:
                return self._call("/send-text", body=body)
            except ZApiHTTPError as err:
                last_err = err
                if err.status == 400 and _is_smartphone_offline(err.body):
                    if attempt < len(_RESTART_BACKOFF_S):
                        wait = _RESTART_BACKOFF_S[attempt]
                        log.warning("smartphone offline; restart + espera %ds", wait)
                        try:
                            self.restart()
                        except ZApiError:
                            pass
                        time.sleep(wait)
                        continue
                    raise ZApiSmartphoneOffline(str(err)) from err
                raise  # outro erro HTTP: permanente, não retenta
            except requests.RequestException as err:
                raise ZApiError(f"falha de rede no Z-API: {err}") from err
        if last_err:
            raise last_err
        raise ZApiError("send_text esgotou tentativas sem resposta")  # pragma: no cover


class ZApiSender:
    """Sender de WhatsApp ao vivo: pacing anti-ban + delays humanos + SendResult."""

    def __init__(
        self,
        client: ZApiClient,
        *,
        pacing_min_s: int = 20,
        pacing_max_s: int = 45,
    ) -> None:
        self.client = client
        self.pacing_min_s = pacing_min_s
        self.pacing_max_s = pacing_max_s
        self._last_send_ts: float | None = None

    def healthcheck(self) -> dict:
        """Confere conexão antes do lote; levanta ``ZApiNotConnected`` se inapto."""
        status = self.client.get_status()
        if status.get("connected") is False or status.get("needsQrCode") is True:
            raise ZApiNotConnected(f"instância Z-API indisponível: {status}")
        return status

    def _pace(self) -> None:
        if self._last_send_ts is None:
            return
        target = random.uniform(self.pacing_min_s, self.pacing_max_s)
        elapsed = time.monotonic() - self._last_send_ts
        if elapsed < target:
            time.sleep(target - elapsed)

    def send(self, phone_raw: str, message: str) -> SendResult:
        phone = canonical_brazilian_phone(phone_raw)
        if not phone:
            return SendResult(False, Resultado.TELEFONE_INVALIDO, detail=f"inválido: {phone_raw!r}")
        self._pace()
        try:
            resp = self.client.send_text(
                phone,
                message,
                delay_message=random.randint(1, 3),
                delay_typing=random.randint(2, 5),
            )
        except ZApiError as err:
            return SendResult(False, Resultado.ERRO, detail=str(err))
        finally:
            self._last_send_ts = time.monotonic()
        msg_id = resp.get("messageId") or resp.get("id")
        return SendResult(True, Resultado.ENVIADO, message_id=msg_id)


class DryRunWhatsApp:
    """Sender de WhatsApp em dry-run: não envia, devolve ``DRY_RUN``."""

    def healthcheck(self) -> dict:  # no-op no dry-run
        return {"dry_run": True}

    def send(self, phone_raw: str, message: str) -> SendResult:
        phone = canonical_brazilian_phone(phone_raw)
        if not phone:
            return SendResult(False, Resultado.TELEFONE_INVALIDO, detail=f"inválido: {phone_raw!r}")
        return SendResult(False, Resultado.DRY_RUN, detail="dry-run: não enviado")


__all__ = [
    "ZApiClient",
    "ZApiSender",
    "DryRunWhatsApp",
    "SendResult",
    "ZApiError",
    "ZApiHTTPError",
    "ZApiSmartphoneOffline",
    "ZApiNotConnected",
]
