"""Modos de calibração: ``--inspect`` (mapear DOM) e ``--validate-selectors``.

Ambos são READ-ONLY (nunca mutam a MAG) e seguros em qualquer modo.

O dump precisa ATRAVESSAR o shadow DOM (o Lightning usa shadow roots abertos em
quase tudo), então:
- ``aria_snapshot`` (árvore de acessibilidade) atravessa shadow DOM;
- um JS recursivo coleta elementos interativos entrando em cada ``shadowRoot``.
``page.content()`` sozinho NÃO traz o conteúdo dentro de shadow roots.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeout

from ...clock import now_utc
from ...cpf import format_cpf

log = logging.getLogger("seguros.mag.inspect")

# Coleta (atravessando shadow DOM) elementos úteis para escolher seletores.
_JS_COLLECT = """
() => {
  const out = [];
  const seen = new Set();
  const wanted = new Set(['button','a','input','select','textarea','th','label','option']);
  function walk(root) {
    let els;
    try { els = root.querySelectorAll('*'); } catch (e) { return; }
    for (const el of els) {
      try {
        const tag = (el.tagName || '').toLowerCase();
        const role = el.getAttribute ? (el.getAttribute('role') || '') : '';
        const text = ((el.innerText || el.value || '') + '').trim().slice(0, 90);
        const aria = el.getAttribute ? (el.getAttribute('aria-label') || '') : '';
        const ph = el.getAttribute ? (el.getAttribute('placeholder') || '') : '';
        const title = el.getAttribute ? (el.getAttribute('title') || '') : '';
        const interactive = role || wanted.has(tag) || tag.startsWith('lightning-');
        if (interactive && (text || aria || ph || title)) {
          const key = tag + '|' + role + '|' + text + '|' + aria;
          if (!seen.has(key)) {
            seen.add(key);
            out.push({ tag, role, text, aria, placeholder: ph, title });
          }
        }
        if (el.shadowRoot) walk(el.shadowRoot);
      } catch (e) { /* ignora */ }
    }
  }
  walk(document);
  return out;
}
"""


def run_inspect(connector, artifacts_dir: Path) -> Path:
    """Navega às páginas-alvo e despeja artefatos para calibrar ``selectors.yaml``."""
    connector.ensure_authenticated(interactive=True)
    stamp = now_utc().strftime("%Y%m%d-%H%M%S")
    out = artifacts_dir / "inspect" / stamp
    out.mkdir(parents=True, exist_ok=True)
    page = connector.page

    targets = {
        "inadimplencias": connector.cfg.mag_inadimplencias_url,
        "clientes": connector.cfg.mag_clientes_url,
    }
    for nome, url in targets.items():
        try:
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)  # respiro p/ o Lightning renderizar a tabela
        except (PlaywrightTimeout, PlaywrightError) as err:
            log.warning("falha ao abrir %s: %s", nome, err)
        _dump_page(page, out, nome)
        log.info("dump de '%s' salvo", nome)

    _capture_details(connector, out)

    print(f"\nArtefatos de inspeção salvos em: {out}")
    print("Pode FECHAR o navegador/Inspector — os arquivos já foram salvos.")
    try:
        page.pause()
    except Exception:  # noqa: BLE001 - fechar a janela durante o pause é ok
        pass
    return out


def _capture_details(connector, out: Path) -> None:
    """Abre o detalhe do 1º inadimplente e o cliente correspondente, e dumpa.

    READ-ONLY: abre detalhe e a aba 'Não trabalhadas'. NUNCA clica em 'Cobrar'.
    """
    sel = connector.selectors
    page = connector.page
    try:
        dels = connector.discover_delinquents()
    except Exception as err:  # noqa: BLE001
        log.warning("discover_delinquents falhou na captura de detalhe: %s", err)
        dels = []
    if not dels:
        log.warning("sem inadimplentes para capturar telas de detalhe")
        return
    cpf = dels[0].cpf
    log.info("capturando detalhe (cpf=***%s)", cpf[-4:])

    # 1) detalhe da INADIMPLÊNCIA (abas, checkbox, botão Cobrar) — SEM clicar Cobrar
    try:
        connector.session.goto(connector.cfg.mag_inadimplencias_url)
        page.wait_for_timeout(2500)
        if connector._open_inadimplencia_detail(cpf):
            page.wait_for_timeout(2500)
            _dump_page(page, out, "inad_detail")
            _safe(lambda: sel.locator(page, "detail.tab_nao_trabalhadas").first.click(),
                  "click tab nao_trabalhadas")
            page.wait_for_timeout(2000)
            _dump_page(page, out, "inad_detail_nao_trabalhadas")
        else:
            log.warning("não consegui abrir o detalhe da inadimplência")
    except Exception as err:  # noqa: BLE001
        log.warning("captura de detalhe da inadimplência falhou: %s", err)

    # 2) detalhe do CLIENTE (contato + consentimento) via busca por CPF
    try:
        connector.session.goto(connector.cfg.mag_clientes_url)
        page.wait_for_timeout(2000)
        _safe(lambda: _buscar_cliente(sel, page, cpf), "busca cliente")
        page.wait_for_timeout(2500)
        _dump_page(page, out, "cliente_busca")
        if connector._open_cliente_detail(cpf):
            page.wait_for_timeout(2500)
            _dump_page(page, out, "cliente_detail")
        else:
            log.warning("não consegui abrir o detalhe do cliente")
    except Exception as err:  # noqa: BLE001
        log.warning("captura de detalhe do cliente falhou: %s", err)


def _buscar_cliente(sel, page, cpf: str) -> None:
    box = sel.locator(page, "clientes.search_input").first
    box.fill(format_cpf(cpf))
    box.press("Enter")


def _dump_page(page, out: Path, nome: str) -> None:
    _safe(lambda: (out / f"{nome}.html").write_text(page.content(), encoding="utf-8"),
          f"html:{nome}")
    _safe(lambda: page.screenshot(path=str(out / f"{nome}.png"), full_page=True),
          f"screenshot:{nome}")
    _safe(lambda: (out / f"{nome}.frames.json").write_text(
        json.dumps([{"name": f.name, "url": f.url} for f in page.frames], indent=2),
        encoding="utf-8"), f"frames:{nome}")
    _safe(lambda: (out / f"{nome}.aria.yaml").write_text(
        page.locator("body").aria_snapshot(), encoding="utf-8"), f"aria:{nome}")
    _safe(lambda: (out / f"{nome}.elements.json").write_text(
        json.dumps(page.evaluate(_JS_COLLECT), ensure_ascii=False, indent=2),
        encoding="utf-8"), f"elements:{nome}")


def _safe(fn, label: str) -> None:
    try:
        fn()
    except Exception as err:  # noqa: BLE001 - dump é best-effort
        log.warning("dump %s falhou: %s", label, err)


def validate_selectors(connector) -> list[tuple[str, bool, str]]:
    """Confere que cada chave de seletor resolve >=1 elemento. Read-only."""
    connector.ensure_authenticated(interactive=True)
    page = connector.page
    sel = connector.selectors
    results: list[tuple[str, bool, str]] = []

    page_for_prefix = {
        "inadimplencias": connector.cfg.mag_inadimplencias_url,
        "clientes": connector.cfg.mag_clientes_url,
        "auth": connector.cfg.mag_inadimplencias_url,
        "common": connector.cfg.mag_inadimplencias_url,
        "detail": connector.cfg.mag_inadimplencias_url,
        "contact": connector.cfg.mag_clientes_url,
        "modal": connector.cfg.mag_inadimplencias_url,
    }
    visited: set[str] = set()

    for key in sel.leaf_selector_keys():
        prefix = key.split(".")[0]
        url = page_for_prefix.get(prefix)
        if url and url not in visited:
            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
                visited.add(url)
            except (PlaywrightTimeout, PlaywrightError):
                pass
        try:
            count = sel.locator(page, key).count()
            results.append((key, count > 0, f"{count} elemento(s)"))
        except Exception as err:  # noqa: BLE001
            results.append((key, False, str(err)))
    return results


__all__ = ["run_inspect", "validate_selectors"]
