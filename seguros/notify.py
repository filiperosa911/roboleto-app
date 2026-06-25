"""Notificação ao corretor (ex.: "sessão MAG expirou").

Canal primário: WhatsApp no próprio número do corretor via Z-API; fallback sempre
para console + log. Nunca levanta exceção — notificar não pode derrubar o run.
"""

from __future__ import annotations

import logging

from .messaging.phone import canonical_brazilian_phone

log = logging.getLogger("seguros.notify")


class NotificationService:
    def __init__(self, *, zapi_client=None, notify_to: str | None = None) -> None:
        self.zapi_client = zapi_client
        self.notify_to = notify_to

    def notify(self, message: str) -> None:
        log.warning("NOTIFICAÇÃO: %s", message)
        print(f"\n🔔 {message}\n")
        if not (self.zapi_client and self.notify_to):
            return
        phone = canonical_brazilian_phone(self.notify_to)
        if not phone:
            log.warning("NOTIFY_WHATSAPP_TO inválido: %r", self.notify_to)
            return
        try:
            self.zapi_client.send_text(phone, f"[Régua MAG] {message}")
        except Exception as err:  # noqa: BLE001 - notificação é best-effort
            log.warning("falha ao notificar por WhatsApp: %s", err)


__all__ = ["NotificationService"]
