"""Utilitários de scraping para o Lightning SPA: esperas, parsing BR e leitura
de datatable virtualizada/paginada.

Tudo com esperas explícitas (sem ``sleep`` fixo) e dedupe por chave de negócio.
Os seletores chegam por chave (``SelectorConfig``); o DOM real é calibrado depois.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation

from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeout

log = logging.getLogger("seguros.mag.scraping")

_NON_DIGITS = re.compile(r"\D")


# --- parsing BR --------------------------------------------------------------


def parse_brl_to_cents(text: str | None) -> int | None:
    """``"R$ 1.234,56"`` -> ``123456`` centavos. ``None`` se não parsear."""
    if not text:
        return None
    cleaned = text.replace("R$", "").strip()
    cleaned = cleaned.replace(".", "").replace(",", ".")
    cleaned = re.sub(r"[^\d.\-]", "", cleaned)
    if not cleaned:
        return None
    try:
        return int((Decimal(cleaned) * 100).quantize(Decimal("1")))
    except (InvalidOperation, ValueError):
        return None


def parse_date_to_iso(text: str | None) -> str | None:
    """Aceita ``dd/mm/aaaa`` ou ``aaaa-mm-dd`` -> ISO ``aaaa-mm-dd``."""
    if not text:
        return None
    t = text.strip()
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", t)
    if m:
        d, mo, y = m.groups()
        return f"{y}-{mo}-{d}"
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        return m.group(0)
    return None


def competencia_from_iso(iso_date: str | None) -> str | None:
    """``"2026-04-10"`` -> ``"04/2026"``."""
    if not iso_date:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", iso_date)
    return f"{m.group(2)}/{m.group(1)}" if m else None


def sim_nao_to_bool(text: str | None) -> bool:
    return bool(text) and "sim" in text.strip().lower()


# --- esperas -----------------------------------------------------------------


def wait_settled(page, selectors, *, settle_timeout_ms: int = 8000) -> None:
    """Espera o spinner do Lightning sumir.

    NÃO espera ``networkidle``: o Salesforce faz polling constante e nunca fica
    ocioso, o que desperdiçava ~10s por chamada. O sinal real é o spinner sumir.
    """
    spinner = selectors.locator(page, "common.spinner")
    try:
        spinner.first.wait_for(state="hidden", timeout=settle_timeout_ms)
    except (PlaywrightTimeout, PlaywrightError):
        pass


# --- leitura de tabela -------------------------------------------------------


def resolve_columns(table, col_map: dict[str, str]) -> dict[str, int]:
    """Mapeia coluna lógica -> índice, a partir do TEXTO do cabeçalho.

    Reordenar colunas na UI não quebra (resolvemos por nome, não por posição).
    """
    headers = [h.inner_text().strip() for h in table.get_by_role("columnheader").all()]
    out: dict[str, int] = {}
    for logical, header_text in col_map.items():
        for idx, h in enumerate(headers):
            if header_text.lower() in h.lower():
                out[logical] = idx
                break
    return out


def _row_cells_text(row) -> list[str]:
    return [c.inner_text().strip() for c in row.get_by_role("cell").all()]


_CPF_PATTERN = re.compile(r"\d{3}\.\d{3}\.\d{3}-\d{2}")


def _wait_for_data_rows(page, selectors, table_key: str, *, timeout_s: int = 25) -> bool:
    """Poll até a tabela conter um CPF de verdade (sinal de que os dados/filtro
    carregaram). Não se deixa enganar pelo 'Nenhum resultado' transitório, que
    aparece antes de o filtro da URL ser aplicado. Retorna False se ficar vazio."""
    for _ in range(timeout_s):
        try:
            txt = selectors.locator(page, table_key).first.inner_text(timeout=2000)
        except (PlaywrightTimeout, PlaywrightError):
            txt = ""
        if _CPF_PATTERN.search(txt):
            return True
        try:
            page.wait_for_timeout(1000)
        except PlaywrightError:
            break
    return False


def scrape_table(
    page,
    selectors,
    *,
    table_key: str,
    row_key: str,
    col_map: dict[str, str],
    key_col: str,
    next_key: str | None = None,
    empty_key: str | None = None,
    max_pages: int = 100,
    scroll_stagnant_limit: int = 3,
) -> list[dict[str, str]]:
    """Lê todas as linhas (virtualização + paginação), dedupe por ``key_col``."""
    wait_settled(page, selectors)

    # O SPA mostra "Nenhum resultado" TRANSITÓRIO antes de aplicar o filtro da
    # URL e carregar os dados. Fazemos poll pelas linhas de dados (>1 = cabeçalho
    # + dados); só aceitamos "vazio" se persistir até o fim do tempo.
    if not _wait_for_data_rows(page, selectors, table_key, timeout_s=25):
        log.debug("nenhuma linha de dados após espera; tabela vazia")
        return []

    table = selectors.locator(page, table_key).first
    columns = resolve_columns(table, col_map)
    if key_col not in columns:
        log.warning("coluna-chave %r não encontrada nos cabeçalhos; verifique selectors.yaml", key_col)

    results: list[dict[str, str]] = []
    seen: set[str] = set()

    for _page_idx in range(1, max_pages + 1):
        before = len(results)
        _scroll_collect(table, columns, key_col, seen, results, scroll_stagnant_limit)
        # Parada robusta por "Mostrando 1 a Y de Z resultados": se já lemos tudo.
        _y, z = _parse_mostrando(page, selectors)
        if z is not None and len(results) >= z:
            break
        if not next_key:
            break
        nxt = selectors.locator(page, next_key).first
        adv = _can_advance(nxt)
        prev_sel = _selected_page(page)
        prev_first = _first_cpf(selectors, page, table_key)
        log.debug("paginação: página %s, %d resultado(s), +%d novos",
                  prev_sel, len(results), len(results) - before)
        if not adv:
            break
        try:
            nxt.click()
        except PlaywrightError as err:
            log.debug("clique no próximo falhou: %s", err)
            break
        # troca confirmada quando a página selecionada E os dados (1º CPF) mudam
        changed = _wait_page_changed(page, selectors, table_key, prev_sel, prev_first)
        log.debug("page_changed=%s (de página %s)", changed, prev_sel)
        if not changed:
            break
        table = selectors.locator(page, table_key).first
    log.debug("scrape_table: %d resultado(s) em até %d página(s)", len(results), _page_idx)
    return results


def _selected_page(page) -> str | None:
    """Número (data-page) do botão de página atualmente selecionado."""
    try:
        btn = page.locator("button.pagination-button.page.selected").first
        return btn.get_attribute("data-page", timeout=2000)
    except (PlaywrightTimeout, PlaywrightError):
        return None


def _first_cpf(selectors, page, table_key: str) -> str:
    """1º CPF mostrado na tabela (1ª linha de dados)."""
    try:
        txt = selectors.locator(page, table_key).first.inner_text(timeout=2000)
    except (PlaywrightTimeout, PlaywrightError):
        return ""
    m = _CPF_PATTERN.search(txt)
    return m.group(0) if m else ""


def _wait_page_changed(page, selectors, table_key: str, prev_sel: str | None,
                       prev_first: str, *, timeout_s: int = 20) -> bool:
    """Confirma a troca de página: o botão SELECIONADO mudou E os DADOS (1º CPF)
    renderizaram diferentes (a página selecionada muda na hora, mas os dados
    demoram a renderizar)."""
    for _ in range(timeout_s):
        sel_changed = (_selected_page(page) or prev_sel) != prev_sel
        cur_first = _first_cpf(selectors, page, table_key)
        if sel_changed and cur_first and cur_first != prev_first:
            return True
        try:
            page.wait_for_timeout(1000)
        except PlaywrightError:
            break
    return False


_MOSTRANDO_RE = re.compile(r"de\s+(\d+)\s+resultado", re.IGNORECASE)


def _parse_mostrando(page, selectors) -> tuple[int | None, int | None]:
    """Lê o total Z de 'Mostrando 1 a Y de Z resultados'. Retorna (Y, Z)."""
    try:
        txt = selectors.locator(page, "inadimplencias.pagination_status").first.inner_text(
            timeout=2000
        )
    except (PlaywrightTimeout, PlaywrightError):
        return None, None
    m = _MOSTRANDO_RE.search(txt)
    return None, (int(m.group(1)) if m else None)


def _scroll_collect(table, columns, key_col, seen, results, stagnant_limit) -> None:
    stagnant = 0
    while stagnant < stagnant_limit:
        rows = table.get_by_role("row")
        novos = 0
        for i in range(rows.count()):
            row = rows.nth(i)
            cells = _row_cells_text(row)
            if not cells:
                continue
            rec = {logical: cells[idx] for logical, idx in columns.items() if idx < len(cells)}
            key = _NON_DIGITS.sub("", rec.get(key_col, "")) if key_col in rec else ""
            if not key or key in seen:
                continue
            seen.add(key)
            results.append(rec)
            novos += 1
        stagnant = stagnant + 1 if novos == 0 else 0
        try:
            rows.last.scroll_into_view_if_needed(timeout=3000)
        except (PlaywrightTimeout, PlaywrightError):
            break


def _can_advance(next_locator) -> bool:
    try:
        if not next_locator.is_visible():
            return False
        if not next_locator.is_enabled():
            return False
        aria = next_locator.get_attribute("aria-disabled")
        return aria != "true"
    except PlaywrightError:
        return False


__all__ = [
    "parse_brl_to_cents",
    "parse_date_to_iso",
    "competencia_from_iso",
    "sim_nao_to_bool",
    "wait_settled",
    "resolve_columns",
    "scrape_table",
]
