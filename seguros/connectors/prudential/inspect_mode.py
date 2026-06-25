"""Calibração da Prudential: ``--inspect`` (mapear o DOM) e ``--validate-selectors``.

READ-ONLY. O "Filtrar" do relatório é seguro (só consulta), então o inspect o
aciona para capturar a GRADE de resultados — que é o que falta para calibrar.
Captura o iframe (#iframe-adobe -> ASPX) e também tenta dumpar o conteúdo do
frame, já que o formulário/grade vivem lá dentro.

Reusa o dumper genérico da MAG (html/screenshot/aria/elements/frames).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeout

from ...clock import now_utc
from ..mag.inspect_mode import _dump_page, _safe

log = logging.getLogger("seguros.prudential.inspect")

# Enumera os campos do form (top-level) com id/name/value/rótulo — base p/ calibrar.
_FORM_ELEMENTS_JS = r"""
() => {
  const lab = (el) => {
    let t = '';
    if (el.id) { const l = document.querySelector(`label[for="${el.id}"]`); if (l) t = l.innerText; }
    if (!t) { const p = el.closest('tr'); if (p) t = (p.innerText || '').slice(0, 50); }
    return (t || '').trim().replace(/\s+/g, ' ');
  };
  return Array.from(document.querySelectorAll('input,select,button')).map(el => ({
    tag: el.tagName.toLowerCase(), type: el.type || '', id: el.id || '',
    name: el.name || '', value: (el.value || '').slice(0, 25), label: lab(el),
  }));
}"""

# Enumera tabelas com >1 linha (cabeçalho + amostra) — confirma a grade/colunas.
_TABLES_JS = r"""
() => Array.from(document.querySelectorAll('table')).map((t, i) => ({
  i, id: t.id || '', rows: t.rows.length,
  head: (t.rows[0] ? Array.from(t.rows[0].cells).map(c => c.innerText.trim()).join(' | ') : ''),
  sample: (t.rows[1] ? Array.from(t.rows[1].cells).map(c => (c.innerText || '').trim().slice(0, 20)).join(' | ') : ''),
})).filter(t => t.rows > 1)"""


def _eval(page, js):
    try:
        return page.evaluate(js)
    except (PlaywrightTimeout, PlaywrightError):
        return []


def capture_form_dom(connector, artifacts_dir: Path) -> Path:
    """Pós-login (sessão fresca): dumpa o form + a grade do Relatório de Atraso,
    SEM pausar, para calibrar os selectors. Read-only (o 'Filtrar' é só consulta).

    Salva ``form.elements.json`` (ids reais dos campos) e ``result.tables.json``
    (cabeçalhos/colunas da grade) — é o que o assistente precisa para calibrar.
    """
    page = connector.page
    out = artifacts_dir / "prudential" / "login-calib"
    out.mkdir(parents=True, exist_ok=True)
    try:
        connector.session.goto(connector.cfg.prudential_atraso_url)
        page.wait_for_timeout(3500)
    except (PlaywrightTimeout, PlaywrightError):
        pass
    if "sso" in (page.url or "").lower():
        log.warning("capture_form_dom: sessão caiu no SSO; nada a capturar")
        return out

    _safe(lambda: (out / "form.html").write_text(page.content(), encoding="utf-8"), "form.html")
    els = _eval(page, _FORM_ELEMENTS_JS)
    _safe(lambda: (out / "form.elements.json").write_text(
        json.dumps(els, ensure_ascii=False, indent=1), encoding="utf-8"), "elements")

    # Heurística p/ exercitar o filtro (e capturar a grade): acha o campo "Dias
    # Atraso" pelo rótulo e o botão "Filtrar" pelo value.
    dias = next((e for e in els if e["tag"] == "input" and e["type"] == "text"
                 and "dias atraso" in e["label"].lower()), None)
    filt = next((e for e in els if "filtrar" in (e.get("value") or "").lower()), None)
    if dias and dias["id"]:
        _safe(lambda: page.locator(f"#{dias['id']}").fill("1"), "fill dias_atraso")
    if filt and (filt["id"] or filt["name"]):
        sel = f"#{filt['id']}" if filt["id"] else f"[name=\"{filt['name']}\"]"
        _safe(lambda: page.locator(sel).first.click(), "click filtrar")
        try:
            page.wait_for_load_state("networkidle", timeout=25000)
        except (PlaywrightTimeout, PlaywrightError):
            pass
        page.wait_for_timeout(3500)

    _safe(lambda: (out / "result.html").write_text(page.content(), encoding="utf-8"), "result.html")
    _safe(lambda: (out / "result.tables.json").write_text(
        json.dumps(_eval(page, _TABLES_JS), ensure_ascii=False, indent=1), encoding="utf-8"), "tables")
    print(f"\n📸 Calibração capturada em: {out}")
    print("   (form.elements.json = ids reais dos campos; result.tables.json = grade)")
    print("   Pode fechar o navegador. Me avise que eu calibro o selectors.yaml.")
    return out


def run_inspect(connector, artifacts_dir: Path) -> Path:
    """Abre o Relatório de Atraso, filtra e despeja artefatos para conferência."""
    connector.ensure_authenticated(interactive=True)
    stamp = now_utc().strftime("%Y%m%d-%H%M%S")
    out = artifacts_dir / "prudential" / "inspect" / stamp
    out.mkdir(parents=True, exist_ok=True)
    page = connector.page

    # 1) Página do relatório (casca + iframe).
    try:
        connector.session.goto(connector.cfg.prudential_atraso_url)
        page.wait_for_timeout(3000)
    except (PlaywrightTimeout, PlaywrightError) as err:
        log.warning("falha ao abrir o relatório de atraso: %s", err)
    _dump_page(page, out, "atraso_form")
    _dump_frames(page, out, "atraso_form")

    # 2) Aciona "Filtrar" (read-only) para renderizar a GRADE e capturá-la.
    _safe(lambda: connector._fill_and_filter(), "fill+filtrar")
    page.wait_for_timeout(3500)
    _dump_page(page, out, "atraso_result")
    _dump_frames(page, out, "atraso_result")

    print(f"\nArtefatos de inspeção da Prudential salvos em: {out}")
    print("A grade se auto-calibra (acha a tabela pelo CPF). Se o discover não "
          "achar a grade ou não preencher 'Dias Atraso', confira o form nos "
          ".frame-*.html (o form/grade está no iframe) e ajuste atraso.form.* no "
          "selectors.yaml.")
    try:
        page.pause()
    except Exception:  # noqa: BLE001
        pass
    return out


def _dump_frames(page, out: Path, nome: str) -> None:
    """Despeja o HTML de cada frame (o form/grade está no #iframe-adobe)."""
    for i, fr in enumerate(page.frames):
        if fr == page.main_frame:
            continue
        _safe(
            lambda fr=fr, i=i: (out / f"{nome}.frame-{i}.html").write_text(
                fr.content(), encoding="utf-8"
            ),
            f"frame-html:{nome}:{i}",
        )


def validate_selectors(connector) -> list[tuple[str, bool, str]]:
    """Confere que cada chave de seletor resolve >=1 elemento. Read-only."""
    connector.ensure_authenticated(interactive=True)
    page = connector.page
    sel = connector.selectors
    try:
        connector.session.goto(connector.cfg.prudential_atraso_url)
        page.wait_for_timeout(2500)
        _safe(lambda: connector._fill_and_filter(), "fill+filtrar")
        page.wait_for_timeout(2500)
    except (PlaywrightTimeout, PlaywrightError):
        pass

    results: list[tuple[str, bool, str]] = []
    for key in sel.leaf_selector_keys():
        try:
            count = sel.locator(page, key).count()
            results.append((key, count > 0, f"{count} elemento(s)"))
        except Exception as err:  # noqa: BLE001
            results.append((key, False, str(err)))
    return results


__all__ = ["run_inspect", "validate_selectors", "capture_form_dom"]
