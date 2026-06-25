"""Selectors da Prudential — reusa o resolver genérico da MAG, apontando para o
``selectors.yaml`` desta seguradora.

O ``SelectorConfig`` já é agnóstico de seguradora e suporta ``frame:`` (iframe),
o que é necessário aqui caso o relatório precise ser acessado dentro do iframe
AEM (#iframe-adobe) em vez de direto no ASPX.
"""

from __future__ import annotations

from pathlib import Path

from ..mag.selectors import SelectorConfig, SelectorConfigError

_SELECTORS_FILE = Path(__file__).with_name("selectors.yaml")


def load_selectors() -> SelectorConfig:
    return SelectorConfig(path=_SELECTORS_FILE)


__all__ = ["SelectorConfig", "SelectorConfigError", "load_selectors"]
