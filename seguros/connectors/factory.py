"""Factory de conector por seguradora.

O dashboard pode hospedar mais de uma seguradora (selecionada na entrada). Este
é o ÚNICO ponto que resolve ``config.insurer`` -> classe de conector concreta,
para que ``worker.py`` (e quem mais precisar) não importe MAG/Prudential direto.
"""

from __future__ import annotations

# Seguradoras suportadas pelo dashboard (ordem = ordem de exibição no seletor).
SUPPORTED_INSURERS: tuple[str, ...] = ("mag", "prudential")

INSURER_LABELS: dict[str, str] = {
    "mag": "MAG Seguros",
    "prudential": "Prudential",
}

# Capability: a seguradora tem link/2ª via de pagamento gerável no portal?
# MAG = sim (magpag). Prudential = não conhecido (provável débito automático) ->
# a régua atua como LEMBRETE (sem link, sem exigir link no gate de envio).
_HAS_PAYMENT_LINK: dict[str, bool] = {"mag": True, "prudential": False}


def insurer_has_payment_link(insurer: str) -> bool:
    return _HAS_PAYMENT_LINK.get((insurer or "mag").lower(), True)


def build_connector(config, notifier=None):
    """Instancia o conector da seguradora em ``config.insurer``."""
    insurer = (getattr(config, "insurer", "mag") or "mag").lower()
    if insurer == "mag":
        from .mag.connector import MagConnector

        return MagConnector(config, notifier=notifier)
    if insurer == "prudential":
        from .prudential.connector import PrudentialConnector

        return PrudentialConnector(config, notifier=notifier)
    raise ValueError(f"seguradora não suportada: {insurer!r}")


__all__ = [
    "SUPPORTED_INSURERS",
    "INSURER_LABELS",
    "build_connector",
    "insurer_has_payment_link",
]
