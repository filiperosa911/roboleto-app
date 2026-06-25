"""``MagConnector`` — implementação MAG da fronteira ``SeguradoraConnector``.

Toda a fragilidade de DOM fica aqui e nos módulos vizinhos (``session``,
``scraping``, ``links``, ``selectors.yaml``). O resto do app não conhece nada disto.

Calibrado e validado em dry-run real (2026-06-19). O fluxo do modal "Gerar link
de pagamento" só é exercido em ``--live`` (clica "Cobrar"), então seus seletores
serão confirmados no primeiro ``--live --limit 1``.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from urllib.parse import urljoin

from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeout

from ...clock import now_utc
from ...cpf import format_cpf, normalize_cpf
from ..base import (
    ClientNotFoundError,
    ClientStatus,
    CompetenciaStatus,
    Contact,
    Delinquent,
    PaymentLinkResult,
    SeguradoraConnector,
    Situacao,
    WorkStatus,
)
from . import links
from .scraping import (
    competencia_from_iso,
    parse_brl_to_cents,
    parse_date_to_iso,
    scrape_table,
    sim_nao_to_bool,
    wait_settled,
)
from .selectors import SelectorConfig
from .session import MagSession

log = logging.getLogger("seguros.mag.connector")


def _map_work_status(text: str | None) -> WorkStatus:
    # Remove acentos: "Não Trabalhado" -> "nao trabalhado" (senão o substring
    # "trabalhado" capturaria "Não Trabalhado" como TRABALHADO).
    t = unicodedata.normalize("NFKD", (text or "").lower())
    key = "".join(ch for ch in t if not unicodedata.combining(ch)).replace(" ", "").replace("-", "")
    if "naotrabalhado" in key:
        return WorkStatus.NAO_TRABALHADO
    if "parcial" in key:
        return WorkStatus.TRABALHADO_PARCIALMENTE
    if "trabalhado" in key:
        return WorkStatus.TRABALHADO
    return WorkStatus.UNKNOWN


class MagConnector(SeguradoraConnector):
    name = "MAG"

    def __init__(self, config, *, notifier=None, selectors: SelectorConfig | None = None) -> None:
        self.cfg = config
        self.selectors = selectors or SelectorConfig()
        self.session = MagSession(config, self.selectors, notifier=notifier)
        # Cache do href "Ver perfil do cliente" por CPF: evita reabrir o detalhe
        # da inadimplência no fetch_contact (otimização — ~metade do tempo/cliente).
        self._profile_href: dict[str, str] = {}

    # --- ciclo de vida -------------------------------------------------------

    def start(self) -> None:
        self.session.start()

    def close(self) -> None:
        self.session.close()

    @property
    def page(self):
        return self.session.page

    def ensure_authenticated(self, *, interactive: bool) -> None:
        self.session.ensure_authenticated(interactive=interactive)

    # --- passo 2: descoberta -------------------------------------------------

    def discover_delinquents(self) -> list[Delinquent]:
        self.session.goto(self.cfg.mag_inadimplencias_url)
        wait_settled(self.page, self.selectors)
        rows = scrape_table(
            self.page,
            self.selectors,
            table_key="inadimplencias.table",
            row_key="inadimplencias.row",
            col_map=self.selectors.get("inadimplencias.col", {}),
            key_col="cpf",
            next_key="inadimplencias.pagination_next",
            empty_key="inadimplencias.empty_state",
        )
        log.debug("scrape_table devolveu %d linha(s) brutas", len(rows))
        if not rows:
            self._debug_shot("discovery_empty")
        out: list[Delinquent] = []
        for r in rows:
            cpf = normalize_cpf(r.get("cpf"))
            if len(cpf) != 11:
                continue
            venc_iso = parse_date_to_iso(r.get("vencimento"))
            out.append(
                Delinquent(
                    cpf=cpf,
                    nome=(r.get("nome") or "").strip(),
                    vencimento_mais_antigo=venc_iso,
                    valor_total_cents=parse_brl_to_cents(r.get("valor")),
                    valor_texto=r.get("valor"),
                    competencia=competencia_from_iso(venc_iso),
                    status=_map_work_status(r.get("status")),
                    raw=r,
                )
            )
        return out

    # --- passo: contato + consentimento -------------------------------------

    def fetch_contact(self, cpf: str) -> Contact:
        cpf = normalize_cpf(cpf)
        # 1) se generate_payment_link já abriu o detalhe e cacheou o href do
        #    perfil, vamos direto (sem reabrir o detalhe da inadimplência).
        href = self._profile_href.pop(cpf, None)
        if href:
            self.session.goto(urljoin(self.cfg.mag_inadimplencias_url, href))
            wait_settled(self.page, self.selectors)
        # 2) senão, caminho determinístico via detalhe; 3) fallback: busca.
        elif not self._goto_client_detail_via_inadimplencia(cpf):
            if not self._goto_client_detail_via_search(cpf):
                raise ClientNotFoundError(cpf)
        wait_settled(self.page, self.selectors)

        # Os campos ficam em <div class="item"><div class="label">..<div class="value">..
        # (a seção "Contato" pode estar recolhida, mas os valores já estão no DOM).
        return Contact(
            cpf=cpf,
            email=self._read_item_value("E-mail"),
            celular=self._read_item_value("Celular"),
            telefone=self._read_item_value("Telefone"),
            autoriza_whatsapp=sim_nao_to_bool(self._read_item_value("Autoriza envio de WhatsApp")),
            autoriza_email=sim_nao_to_bool(self._read_item_value("Autoriza envio de e-mail")),
            autoriza_sms=sim_nao_to_bool(self._read_item_value("Autoriza envio de SMS")),
        )

    def _cache_profile_href(self, cpf: str) -> None:
        """Lê o href de 'Ver perfil do cliente' na tela de detalhe (se houver)."""
        href = self._read_profile_href()
        if href:
            self._profile_href[cpf] = href

    def _read_profile_href(self) -> str | None:
        try:
            link = self.page.get_by_role("link", name="Ver perfil do cliente").first
            link.wait_for(state="attached", timeout=8000)
            return link.get_attribute("href")
        except (PlaywrightTimeout, PlaywrightError):
            return None

    def _goto_client_detail_via_inadimplencia(self, cpf: str) -> bool:
        """Abre o detalhe da inadimplência e navega para o perfil do cliente."""
        self.session.goto(self.cfg.mag_inadimplencias_url)
        if not self._open_inadimplencia_detail(cpf):
            return False
        wait_settled(self.page, self.selectors)
        href = self._read_profile_href()
        if not href:
            log.debug("link 'Ver perfil do cliente' não encontrado")
            return False
        self.session.goto(urljoin(self.page.url, href))
        wait_settled(self.page, self.selectors)
        return True

    def _goto_client_detail_via_search(self, cpf: str) -> bool:
        """Fallback: busca o CPF em Meus Clientes e abre o detalhe."""
        self.session.goto(self.cfg.mag_clientes_url)
        wait_settled(self.page, self.selectors)
        try:
            search = self.selectors.locator(self.page, "clientes.search_input").first
            search.fill(format_cpf(cpf))
            search.press("Enter")
            wait_settled(self.page, self.selectors)
        except PlaywrightError:
            log.warning("campo de busca de clientes não encontrado")
        return self._open_cliente_detail(cpf)

    def _read_item_value(self, label_text: str) -> str | None:
        """Lê o ``.value`` do ``.item`` cujo ``.label`` é exatamente ``label_text``."""
        try:
            item = self.page.locator("div.item").filter(
                has=self.page.get_by_text(label_text, exact=True)
            ).first
            item.wait_for(state="attached", timeout=5000)
            txt = (item.locator("div.value").first.text_content() or "").strip()
            return txt or None
        except (PlaywrightTimeout, PlaywrightError):
            return None

    # --- passo C: gerar link -------------------------------------------------

    def generate_payment_link(self, cpf: str, *, live: bool) -> PaymentLinkResult:
        cpf = normalize_cpf(cpf)
        self.session.goto(self.cfg.mag_inadimplencias_url)
        if not self._open_inadimplencia_detail(cpf):
            log.warning("detalhe de inadimplência não aberto para cpf=%s (calibrar)", cpf)
            return PaymentLinkResult(cpf, link=None, dry_run=not live, would_generate=False)

        # Cacheia o link do perfil do cliente (está nesta tela) para o fetch_contact.
        self._cache_profile_href(cpf)

        would_generate = links.open_nao_trabalhadas_and_select_all(
            self.page, self.selectors, select_all=live
        )
        if not live:
            # DRY-RUN: NÃO marca checkbox nem clica "Cobrar" -> zero mutação na MAG.
            return PaymentLinkResult(cpf, link=None, dry_run=True, would_generate=would_generate)

        link = links.cobrar_and_capture(self.page, self.selectors)
        return PaymentLinkResult(cpf, link=link, dry_run=False, generated_at=now_utc())

    # --- passo: re-check de status ------------------------------------------

    def check_status(self, cpf: str) -> ClientStatus:
        cpf = normalize_cpf(cpf)
        self.session.goto(self.cfg.mag_inadimplencias_url)
        if not self._open_inadimplencia_detail(cpf):
            return ClientStatus(cpf, competencias=(), all_regularized=False, checked_at=now_utc())
        wait_settled(self.page, self.selectors)

        # O cabeçalho do detalhe traz contadores:
        #   "N competência(s) Não trabalhada(s)" / "Trabalhada(s)" / "Regularizada(s)"
        # Resolvido = nenhuma competência em aberto (não trabalhada) nem em cobrança (trabalhada).
        try:
            text = self.page.locator("body").inner_text(timeout=5000)
        except (PlaywrightTimeout, PlaywrightError):
            text = ""
        nao_trab = _count_competencias(text, "Não trabalhada")
        trab = _count_competencias(text, "Trabalhada")
        regular = _count_competencias(text, "Regularizada")
        all_reg = (nao_trab == 0 and trab == 0) and regular >= 0 and bool(text)
        comp = CompetenciaStatus(
            competencia="resumo",
            situacao=Situacao.REGULARIZADA if all_reg else Situacao.EM_ABERTO,
            valor_cents=None,
        )
        return ClientStatus(cpf, competencias=(comp,), all_regularized=all_reg,
                            checked_at=now_utc())

    def check_client_inadimplente_cents(self, cpf: str) -> int | None:
        """Lê o "Valor inadimplente" na tela do cliente (Meus Clientes -> detalhe).
        Retorna centavos (0 = pagou tudo) ou None se não conseguir ler.

        Sinal CONFIÁVEL de pagamento: a tela do cliente é acessível mesmo depois de
        o inadimplente sair da lista de inadimplência (que ocorre já ao "Cobrar").
        """
        cpf = normalize_cpf(cpf)
        if not self._goto_client_detail_via_search(cpf):
            return None
        wait_settled(self.page, self.selectors)
        try:
            item = self.page.locator("c-consolidated-item").filter(
                has_text="Valor inadimplente"
            ).first
            txt = item.inner_text(timeout=6000)
        except (PlaywrightTimeout, PlaywrightError):
            return None
        return _parse_valor_brl(txt)

    # --- helpers DOM ---------------------------------------------------------

    def _open_cliente_detail(self, cpf: str) -> bool:
        return self._open_detail_by_cpf(cpf)

    def _open_inadimplencia_detail(self, cpf: str) -> bool:
        return self._open_detail_by_cpf(cpf)

    def _open_detail_by_cpf(self, cpf: str) -> bool:
        """Localiza a linha cujo CPF casa e clica a seta de detalhe (>).

        Casa preferindo o CPF FORMATADO (123.456.789-09) presente no texto da linha
        — mais preciso que um substring de dígitos solto (que poderia casar a linha
        errada e, em live, cobrar o cliente errado).
        """
        if not cpf:
            return False
        cpf_fmt = format_cpf(cpf)
        # Poll: a linha do CPF pode demorar a aparecer (resultado de busca /
        # filtro carregam async). Esperamos até ~20s pela linha casar.
        for tentativa in range(20):
            wait_settled(self.page, self.selectors)
            try:
                rows = self.selectors.locator(self.page, "inadimplencias.row")
                total = rows.count()
                for i in range(total):
                    row = rows.nth(i)
                    text = row.inner_text() or ""
                    if cpf_fmt not in text and cpf not in normalize_cpf(text):
                        continue
                    log.debug("linha %d casou com %s (tentativa %d)", i, cpf_fmt, tentativa)
                    if not self._click_detail_arrow(row):
                        log.debug("clique na seta falhou para a linha %d", i)
                        self._debug_shot(f"detail_click_fail_{cpf[-4:]}")
                        return False
                    wait_settled(self.page, self.selectors)
                    return True
            except PlaywrightError as err:
                log.debug("erro iterando linhas: %s", err)
            try:
                self.page.wait_for_timeout(1000)
            except PlaywrightError:
                break
        log.debug("nenhuma linha casou com %s após poll", cpf_fmt)
        self._debug_shot(f"detail_no_match_{cpf[-4:]}")
        return False

    @staticmethod
    def _click_detail_arrow(row) -> bool:
        strategies = (
            ("cell.last img", lambda: row.get_by_role("cell").last.locator("img").last.click(timeout=4000)),
            ("cell.last", lambda: row.get_by_role("cell").last.click(timeout=4000)),
            ("row.last button", lambda: row.get_by_role("button").last.click(timeout=4000)),
            ("row click", lambda: row.click(timeout=4000)),
        )
        for nome, attempt in strategies:
            try:
                attempt()
                log.debug("seta clicada via '%s'", nome)
                return True
            except PlaywrightError as err:
                log.debug("estratégia '%s' falhou: %s", nome, str(err).splitlines()[0])
                continue
        return False

    def _debug_shot(self, label: str) -> None:
        try:
            from pathlib import Path

            d = Path("artifacts") / "debug"
            d.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(d / f"{label}.png"), full_page=True)
            (d / f"{label}.html").write_text(self.page.content(), encoding="utf-8")
            log.debug("screenshot de debug salvo: %s", label)
        except Exception:  # noqa: BLE001
            pass


def _parse_valor_brl(text: str) -> int | None:
    """Extrai 'R$ <int>,<dec>' (mesmo com espaços/nbsp) -> centavos. Ex.: 'R$ 0,00' -> 0."""
    t = (text or "").replace("\xa0", " ")
    m = re.search(r"R\$\s*([\d.]+)\s*,\s*(\d{2})", t)
    if not m:
        return None
    return int(m.group(1).replace(".", "")) * 100 + int(m.group(2))


def _count_competencias(text: str, termo: str) -> int:
    """Extrai N de 'N competência(s) <termo>(s)' no texto do detalhe."""
    m = re.search(rf"(\d+)\s*compet[êe]ncia\(s\)\s*{re.escape(termo)}", text)
    return int(m.group(1)) if m else 0


__all__ = ["MagConnector"]
