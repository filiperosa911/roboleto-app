"""Scraping da Prudential (relatório ASPX "Relatório de Atraso").

Reusa os parsers BR da MAG e lê a grade HTML (GridView). Diferença-chave vs MAG:
a Prudential é chaveada por **Apólice** (não CPF) e o telefone/valor vêm na
PRÓPRIA grade (colunas Contatos / Prêmio). A grade não tem id estável, então é
achada pelo cabeçalho-chave ("Apólice"); as colunas são resolvidas por texto.
"""

from __future__ import annotations

import logging
import re

from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeout

# Parsers BR reaproveitados (já cobertos por testes na MAG).
from ..mag.scraping import (  # noqa: F401 - re-exportados para o connector
    competencia_from_iso,
    parse_brl_to_cents,
    parse_date_to_iso,
    sim_nao_to_bool,
)

log = logging.getLogger("seguros.prudential.scraping")

_DIGIT_RUN = re.compile(r"\d+")
_PHONE_RE = re.compile(r"\(?\d{2}\)?\s*9?\d{4}-?\d{4}")
_ABRIR_POP_WIN = re.compile(r"AbrirPopWin\('([^']+)'")

PRUDENTIAL_DBClient_BASE = "https://saa.prudential.com.br/DBClient/"


def extract_boleto_url(onclick: str) -> str | None:
    """Extrai a URL completa do popup de segunda via a partir do onclick do botão."""
    m = _ABRIR_POP_WIN.search(onclick or "")
    if not m:
        return None
    relative = m.group(1)
    if relative.startswith("http"):
        return relative
    return PRUDENTIAL_DBClient_BASE + relative


def extract_apolice_from_boleto_url(url: str) -> str | None:
    """Extrai o número da apólice (parâmetro 'w') da URL do popup."""
    from urllib.parse import parse_qs, urlparse
    qs = parse_qs(urlparse(url).query)
    return (qs.get("w") or [None])[0]


def scrape_boleto_urls(page) -> dict[str, str]:
    """Varre os botões 'Segunda via de Boletos' da grade e retorna {apolice: url}."""
    result: dict[str, str] = {}
    try:
        buttons = page.locator('input[name$="IBT_SegundaViaBoleto"]').all()
    except Exception:
        return result
    for btn in buttons:
        try:
            onclick = btn.get_attribute("onclick", timeout=2000) or ""
            url = extract_boleto_url(onclick)
            if url:
                apolice = extract_apolice_from_boleto_url(url)
                if apolice:
                    result[apolice] = url
        except Exception:
            continue
    log.debug("scrape_boleto_urls: %d botão(ões) encontrado(s)", len(result))
    return result


def longest_digit_run(text: str) -> str:
    """Maior sequência de dígitos (ex.: a Apólice em '74\\n000887908\\nAtiva')."""
    runs = _DIGIT_RUN.findall(text or "")
    return max(runs, key=len) if runs else ""


def extract_phone(text: str) -> str | None:
    """Extrai o telefone de uma célula tipo 'Cel.: (11) 95430-0078'."""
    m = _PHONE_RE.search(text or "")
    return m.group(0).strip() if m else None


def wait_ready(page, selectors, *, timeout_ms: int = 15000) -> None:
    """Espera o relatório assentar: load idle + um marcador sempre-presente."""
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except (PlaywrightTimeout, PlaywrightError):
        pass
    try:
        selectors.locator(page, "common.ready_marker").first.wait_for(
            state="attached", timeout=timeout_ms
        )
    except Exception:  # noqa: BLE001 - marcador é best-effort
        pass


def _header_texts(table) -> list[str]:
    """Textos dos cabeçalhos (th = role columnheader; senão, a 1ª linha)."""
    ths = table.get_by_role("columnheader").all()
    if ths:
        return [h.inner_text().strip() for h in ths]
    rows = table.get_by_role("row")
    if rows.count():
        return [c.inner_text().strip() for c in rows.first.get_by_role("cell").all()]
    return []


def _resolve_columns(headers: list[str], col_map: dict[str, str]) -> dict[str, int]:
    """Coluna lógica -> índice, casando pelo TEXTO do cabeçalho (não posição)."""
    out: dict[str, int] = {}
    for logical, header_text in col_map.items():
        for idx, h in enumerate(headers):
            if header_text.lower() in h.lower():
                out[logical] = idx
                break
    return out


def find_results_table(page, marker: str, *, max_tables: int = 60):
    """Acha a grade de resultados pela presença do cabeçalho-chave (ex.: 'Apólice').
    Entre as tabelas que têm o marcador, escolhe a com mais linhas (a grade real).
    Robusto a ids ausentes/voláteis do ASP.NET."""
    if not marker:
        return None
    tables = page.locator("table")
    try:
        total = min(tables.count(), max_tables)
    except (PlaywrightTimeout, PlaywrightError):
        return None
    best, best_rows = None, 0
    m = marker.lower()
    for i in range(total):
        t = tables.nth(i)
        try:
            if m not in (t.inner_text(timeout=1500) or "").lower():
                continue
            rc = t.locator("tr").count()
        except (PlaywrightTimeout, PlaywrightError):
            continue
        if rc > best_rows:
            best, best_rows = t, rc
    return best


def scrape_grid(
    page,
    selectors,
    *,
    table_key: str,
    col_map: dict[str, str],
    key_col: str = "apolice",
) -> list[dict[str, str]]:
    """Lê a grade do Relatório de Atraso, dedupe pela ``key_col`` (dígitos da
    Apólice). Acha a grade pelo selector calibrado e, se falhar, pelo marcador do
    cabeçalho-chave. Vazio (sem estourar) se não houver grade.
    """
    marker = col_map.get(key_col, "")
    table = None
    try:
        cand = selectors.locator(page, table_key).first
        cand.wait_for(state="attached", timeout=5000)
        if marker and marker.lower() in (cand.inner_text(timeout=2000) or "").lower():
            table = cand
    except Exception:  # noqa: BLE001 - selector pode não bater; cai na heurística
        table = None
    if table is None:
        table = find_results_table(page, marker)
    if table is None:
        log.debug("grade não encontrada (marcador=%r)", marker)
        return []

    headers = _header_texts(table)
    columns = _resolve_columns(headers, col_map)
    if key_col not in columns:
        log.warning("coluna-chave %r (header %r) não mapeada; headers=%r",
                    key_col, marker, headers)
        return []

    results: list[dict[str, str]] = []
    seen: set[str] = set()
    rows = table.get_by_role("row")
    try:
        nrows = rows.count()
    except (PlaywrightTimeout, PlaywrightError):
        return []
    for i in range(nrows):
        try:
            cells = [c.inner_text().strip() for c in rows.nth(i).get_by_role("cell").all()]
        except (PlaywrightTimeout, PlaywrightError):
            continue
        if not cells:
            continue  # cabeçalho (só <th>) ou separador
        rec = {logical: cells[idx] for logical, idx in columns.items() if idx < len(cells)}
        key = longest_digit_run(rec.get(key_col, ""))
        if not key or key in seen:
            continue
        seen.add(key)
        rec[key_col] = key  # normaliza a chave para os dígitos da apólice
        results.append(rec)
    log.debug("scrape_grid: %d apólice(s) em atraso", len(results))
    return results


__all__ = [
    "wait_ready",
    "scrape_grid",
    "find_results_table",
    "longest_digit_run",
    "extract_phone",
    "parse_brl_to_cents",
    "parse_date_to_iso",
    "competencia_from_iso",
    "sim_nao_to_bool",
]
