"""Carga do ``selectors.yaml`` e resolução de chaves lógicas em locators Playwright.

O código nunca contém strings de seletor literais — sempre referencia uma chave
(ex.: ``"detail.cobrar_button"``). Tunar o site = editar o YAML, sem mexer no código.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_SELECTORS_FILE = Path(__file__).with_name("selectors.yaml")


class SelectorConfigError(Exception):
    """Chave ausente ou spec de seletor inválida."""


class SelectorConfig:
    def __init__(self, data: dict | None = None, path: Path | None = None) -> None:
        if data is None:
            path = path or _SELECTORS_FILE
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        self._data = data

    # --- acesso bruto --------------------------------------------------------

    def raw(self, dotted_key: str) -> Any:
        node: Any = self._data
        for part in dotted_key.split("."):
            if not isinstance(node, dict) or part not in node:
                raise SelectorConfigError(f"chave de seletor inexistente: {dotted_key!r}")
            node = node[part]
        return node

    def get(self, dotted_key: str, default: Any = None) -> Any:
        try:
            return self.raw(dotted_key)
        except SelectorConfigError:
            return default

    # --- construção de locator ----------------------------------------------

    def locator(self, page, dotted_key: str):
        """Resolve a chave em um Playwright Locator a partir de ``page`` (ou frame)."""
        spec = self.raw(dotted_key)
        if not isinstance(spec, dict):
            raise SelectorConfigError(f"{dotted_key!r} não é um spec de seletor: {spec!r}")
        root = page
        if "frame" in spec:
            root = page.frame_locator(spec["frame"])
        return self._build(root, spec, dotted_key)

    @staticmethod
    def _build(root, spec: dict, key: str):
        exact = bool(spec.get("exact", False))
        if "role" in spec:
            name = spec.get("name")
            if name is not None:
                return root.get_by_role(spec["role"], name=name, exact=exact)
            return root.get_by_role(spec["role"])
        if "label" in spec:
            return root.get_by_label(spec["label"], exact=exact)
        if "text" in spec:
            return root.get_by_text(spec["text"], exact=exact)
        if "placeholder" in spec:
            return root.get_by_placeholder(spec["placeholder"], exact=exact)
        if "title" in spec:
            return root.get_by_title(spec["title"], exact=exact)
        if "css" in spec:
            return root.locator(spec["css"])
        raise SelectorConfigError(f"spec sem estratégia reconhecida em {key!r}: {spec!r}")

    # --- introspecção para o --validate-selectors ----------------------------

    def leaf_selector_keys(self) -> list[str]:
        """Lista as chaves pontilhadas que são specs de seletor (têm role/css/...)."""
        keys: list[str] = []
        strategy = {"role", "label", "text", "placeholder", "title", "css"}

        def walk(node: Any, prefix: str) -> None:
            if isinstance(node, dict):
                if strategy & set(node.keys()):
                    keys.append(prefix.lstrip("."))
                    return
                for k, v in node.items():
                    walk(v, f"{prefix}.{k}")

        walk(self._data, "")
        return keys


__all__ = ["SelectorConfig", "SelectorConfigError"]
