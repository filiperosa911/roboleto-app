"""Fluxo "Cobrar inadimplência" -> "Gerar link de pagamento" e captura do link.

Esta é a ÚNICA via que MUTA a MAG (move competências para "Trabalhadas"). Em
dry-run o conector retorna ANTES de clicar "Cobrar". A captura é em cascata:
(1) interceptar a resposta de rede -> (2) ler input/href do modal -> (3) clipboard.
"""

from __future__ import annotations

import logging
import re

from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeout

from ..base import PaymentLinkNotCapturedError
from .scraping import wait_settled

log = logging.getLogger("seguros.mag.links")

_URL_RE = re.compile(r"https?://[^\s\"'<>\\]+")

# URLs que NÃO são link de pagamento (rodapé social, assets, infra Salesforce).
_DENY_SUBSTR = (
    "facebook.", "instagram.", "linkedin.", "twitter.", "youtube.", "/magseguros",
    "fonts.googleapis", "fonts.gstatic", ".svg", ".png", ".jpg", ".jpeg", ".css",
    ".js", ".woff", ".ico", "salesforce.com", "force.com", "lightning.com",
    "/resource/", "/auraservice", "google-analytics", "googletagmanager", "hotjar",
)


def _is_payment_url(u: str | None) -> bool:
    """True se a URL parece um link de pagamento (não-social, não-asset)."""
    if not u:
        return False
    lu = u.lower()
    return lu.startswith("http") and not any(d in lu for d in _DENY_SUBSTR)


def open_nao_trabalhadas_and_select_all(page, selectors, *, select_all: bool = True) -> bool:
    """Abre a aba "Não trabalhadas" e (opcionalmente) marca todas as inscrições.

    Retorna True se o botão "Cobrar inadimplência" foi localizado (geraria link).
    Em DRY-RUN passe ``select_all=False``: só navega/verifica, sem tocar no checkbox.
    Nada aqui muta o estado no servidor (a mutação é o clique em "Cobrar").
    """
    wait_settled(page, selectors)
    tab = selectors.locator(page, "detail.tab_nao_trabalhadas").first
    try:
        tab.click()
    except PlaywrightError:
        log.warning("aba 'Não trabalhadas' não encontrada (calibrar selectors.yaml)")
        return False
    wait_settled(page, selectors)

    if select_all:
        _check_master(selectors, page)

    cobrar = selectors.locator(page, "detail.cobrar_button").first
    try:
        return cobrar.is_visible()
    except PlaywrightError:
        return False


def _check_master(selectors, page) -> None:
    """Marca o checkbox 'selecionar todos'. Inputs do Lightning costumam ser
    escondidos (opacity:0), então tentamos check() e, se falhar, clicar no
    rótulo/célula ao redor."""
    master = selectors.locator(page, "detail.master_checkbox").first
    try:
        master.wait_for(state="attached", timeout=8000)
    except (PlaywrightTimeout, PlaywrightError):
        log.warning("checkbox-mestre não encontrado (calibrar)")
        return
    try:
        if master.is_checked():
            return
    except PlaywrightError:
        pass
    for attempt in (
        lambda: master.check(timeout=4000),
        lambda: master.click(force=True, timeout=4000),
        lambda: master.locator("xpath=..").click(timeout=4000),
    ):
        try:
            attempt()
            return
        except (PlaywrightTimeout, PlaywrightError):
            continue
    log.warning("não consegui marcar o checkbox-mestre (calibrar)")


def cobrar_and_capture(page, selectors, *, action_timeout_ms: int = 30000) -> str:
    """Clica Cobrar -> Gerar link e captura o link (cascata). Levanta se falhar."""
    captured: dict[str, str] = {}

    def _maybe_capture(response) -> None:
        if "url" in captured:
            return
        try:
            text = response.text()
        except PlaywrightError:
            return
        for m in _URL_RE.finditer(text):
            if _is_payment_url(m.group(0)):
                log.debug("link candidato (rede %s): %s", response.url[:60], m.group(0))
                captured["url"] = m.group(0)
                return

    page.on("response", _maybe_capture)
    link = None
    try:
        cobrar = selectors.locator(page, "detail.cobrar_button").first
        cobrar.click()
        wait_settled(page, selectors, settle_timeout_ms=8000)

        # Sem exigir role=dialog: vamos direto no botão "Gerar link de pagamento".
        gerar = selectors.locator(page, "modal.gerar_link_button").first
        try:
            gerar.wait_for(state="visible", timeout=action_timeout_ms)
        except (PlaywrightTimeout, PlaywrightError):
            log.warning("'Gerar link de pagamento' não apareceu após 'Cobrar' (calibrar)")
        try:
            gerar.click()
        except (PlaywrightTimeout, PlaywrightError):
            pass
        # A geração é ASSÍNCRONA: o botão vira "Carregando...". Espera terminar
        # (ou o link candidato chegar pela rede), depois extrai.
        _wait_link_ready(page, captured, timeout_s=40)
        wait_settled(page, selectors, settle_timeout_ms=8000)
        _debug_modal(page, "after_gerar")  # estado com o link (calibração)

        link = captured.get("url") or _extract_from_dom(page, selectors) or _extract_from_clipboard(
            page, selectors
        )
        if not link or not _is_payment_url(link):
            link = None
            _debug_modal(page, "no_link")  # modal ainda aberto -> salva p/ calibrar
    finally:
        try:
            page.remove_listener("response", _maybe_capture)
        except (PlaywrightError, ValueError):
            pass
        _close_modal(page, selectors)

    if not link or not link.lower().startswith("http"):
        raise PaymentLinkNotCapturedError("não foi possível capturar o link de pagamento")
    return link


def _wait_link_ready(page, captured: dict, *, timeout_s: int = 40) -> None:
    """Espera o 'Carregando...' do 'Gerar link' terminar OU o link chegar pela rede."""
    for _ in range(timeout_s):
        if captured.get("url"):
            return
        try:
            loading = page.get_by_text("Carregando", exact=False).first
            visivel = loading.is_visible()
        except PlaywrightError:
            visivel = False
        if not visivel:
            return
        try:
            page.wait_for_timeout(1000)
        except PlaywrightError:
            return


def _debug_modal(page, label: str = "modal_link") -> None:
    """Salva screenshot + HTML da tela (modal) para calibração."""
    try:
        from pathlib import Path

        d = Path("artifacts") / "debug"
        d.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(d / f"{label}.png"), full_page=True)
        (d / f"{label}.html").write_text(page.content(), encoding="utf-8")
        log.warning("debug do modal salvo em artifacts/debug/%s.*", label)
    except Exception:  # noqa: BLE001
        pass


def _extract_from_dom(page, selectors) -> str | None:
    # 1) inputs (o link costuma vir num campo copiável)
    inputs = page.locator("input")
    for i in range(min(inputs.count(), 40)):
        try:
            val = inputs.nth(i).input_value(timeout=1000)
        except PlaywrightError:
            continue
        if _is_payment_url(val):
            return val
    # 2) âncoras http (filtradas: rodapé social é descartado)
    anchors = page.locator("a[href^='http']")
    for i in range(min(anchors.count(), 80)):
        try:
            href = anchors.nth(i).get_attribute("href")
        except PlaywrightError:
            continue
        if _is_payment_url(href):
            return href
    return None


def _extract_from_clipboard(page, selectors) -> str | None:
    if not selectors.get("modal.has_copy_button"):
        return None
    try:
        selectors.locator(page, "modal.copy_button").first.click()
        val = page.evaluate("navigator.clipboard.readText()")
        if _is_payment_url(val):
            return val
    except PlaywrightError:
        return None
    return None


def _close_modal(page, selectors) -> None:
    try:
        close = selectors.locator(page, "modal.close").first
        if close.is_visible():
            close.click()
    except PlaywrightError:
        pass


__all__ = ["open_nao_trabalhadas_and_select_all", "cobrar_and_capture"]
