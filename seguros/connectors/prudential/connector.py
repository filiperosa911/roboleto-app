"""``PrudentialConnector`` — implementação Prudential da fronteira
``SeguradoraConnector`` (Life Planner AEM + relatório ASPX de atraso).

Esteira (igual à MAG, adaptada ao portal ASPX):
  discover  -> abre o "Relatório de Atraso", filtra por Dias Atraso >= mínimo,
               lê a grade de resultados.
  contact   -> lê telefone/e-mail da própria linha do relatório (se a grade
               trouxer); senão, found=False (fonte de contato a mapear ao vivo).
  link      -> a Prudential NÃO tem link de pagamento conhecido (provável débito
               automático / 2ª via fora do portal). Por ora é LEMBRETE: retorna
               sem link, sem mutar nada. (A definir ao vivo — ver README.)
  status    -> sumiu do Relatório de Atraso = regularizou (pagou).

AUTO-CALIBRAÇÃO: a grade é achada pelo conteúdo (CPF) e a coluna de CPF é
detectada sozinha (``scraping.find_results_table`` / ``detect_cpf_column``), então
não há selector de grade a editar. Como na MAG, o acesso exige login humano (OTP),
feito 1x no ``--login``; sem sessão válida, ``ensure_authenticated`` orienta a logar.
"""

from __future__ import annotations

import logging
import pathlib

from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeout

from ...clock import now_utc
from ...cpf import normalize_cpf
from ..base import (
    ClientStatus,
    CompetenciaStatus,
    ConnectorError,
    Contact,
    Delinquent,
    PaymentLinkResult,
    SeguradoraConnector,
    Situacao,
)
from .scraping import (
    competencia_from_iso,
    extract_phone,
    parse_brl_to_cents,
    parse_date_to_iso,
    scrape_boleto_urls,
    scrape_grid,
    wait_ready,
)
from .selectors import load_selectors
from .session import PrudentialSession

log = logging.getLogger("seguros.prudential.connector")


class PrudentialConnector(SeguradoraConnector):
    name = "prudential"

    def __init__(self, config, *, notifier=None, selectors=None) -> None:
        self.cfg = config
        self.selectors = selectors or load_selectors()
        self.session = PrudentialSession(config, self.selectors, notifier=notifier)
        # Cache da última descoberta (CPF -> linha bruta) p/ o fetch_contact.
        self._last_rows: dict[str, dict] = {}

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

    # --- descoberta ----------------------------------------------------------

    def discover_delinquents(self) -> list[Delinquent]:
        rows = self._run_atraso_report()
        out: list[Delinquent] = []
        self._last_rows = {}
        for r in rows:
            # chave = dígitos da Apólice (normalizados p/ casar o resto do app,
            # que é CPF-cêntrico; aqui a Apólice ocupa o campo-chave).
            key = normalize_cpf(r.get("apolice", ""))
            if not key:
                continue
            self._last_rows[key] = r
            venc_iso = parse_date_to_iso(r.get("vencimento"))
            tel = extract_phone(r.get("telefone", ""))
            out.append(
                Delinquent(
                    cpf=key,
                    nome=(r.get("nome") or "").strip(),
                    vencimento_mais_antigo=venc_iso,
                    valor_total_cents=parse_brl_to_cents(r.get("valor")),
                    valor_texto=r.get("valor"),
                    competencia=competencia_from_iso(venc_iso),
                    telefone=tel,
                    raw=r,
                )
            )
        return out

    def _run_atraso_report(self) -> list[dict]:
        """Abre o relatório, filtra por Dias Atraso >= mínimo e lê a grade."""
        self.session.goto(self.cfg.prudential_atraso_url)
        wait_ready(self.page, self.selectors)
        self._fill_and_filter()
        wait_ready(self.page, self.selectors)
        rows = scrape_grid(
            self.page,
            self.selectors,
            table_key="atraso.table",
            col_map=self.selectors.get("atraso.col", {}),
            key_col="apolice",
        )
        # Injeta a URL de segunda via de boleto em cada linha (captura enquanto a
        # sessão está viva — a URL carrega parâmetros do servidor).
        boleto_urls = scrape_boleto_urls(self.page)
        for row in rows:
            apolice = row.get("apolice", "")
            if apolice in boleto_urls:
                row["boleto_url"] = boleto_urls[apolice]
        return rows

    def _fill_and_filter(self) -> None:
        minimo = str(self.cfg.prudential_dias_atraso_min)
        try:
            campo = self.selectors.locator(self.page, "atraso.form.dias_atraso_de").first
            campo.fill(minimo, timeout=8000)
        except (PlaywrightTimeout, PlaywrightError) as err:
            log.debug("campo 'Dias Atraso' não preenchido (calibrar): %s", err)
        try:
            self.selectors.locator(self.page, "atraso.form.filtrar_button").first.click(
                timeout=8000
            )
        except (PlaywrightTimeout, PlaywrightError) as err:
            log.debug("botão 'Filtrar' não clicado (calibrar): %s", err)

    # --- contato -------------------------------------------------------------

    def fetch_contact(self, cpf: str) -> Contact:
        cpf = normalize_cpf(cpf)
        row = self._last_rows.get(cpf)
        if row is None:
            # ainda não descoberto neste run: roda a descoberta uma vez.
            self.discover_delinquents()
            row = self._last_rows.get(cpf)
        if not row:
            return Contact(cpf=cpf, found=False)
        # O telefone vem na PRÓPRIA grade (coluna Contatos: "Cel.: (11) 9...").
        tel = extract_phone(row.get("telefone", ""))
        return Contact(cpf=cpf, celular=tel, telefone=tel, found=bool(tel))

    # --- segunda via de boleto -----------------------------------------------

    def generate_payment_link(self, cpf: str, *, live: bool) -> PaymentLinkResult:
        """Gera segunda via de boleto pelo portal da Prudential.

        Navega até PAG_DBClient_EmissaoSegundaViaBoleto.aspx, seleciona a
        parcela mais antiga em aberto, clica Imprimir e captura a URL do popup.
        Em dry-run, apenas informa se o botão existe para este cliente.
        """
        cpf = normalize_cpf(cpf)
        row = self._last_rows.get(cpf)
        if not row:
            self.discover_delinquents()
            row = self._last_rows.get(cpf)

        boleto_url = (row or {}).get("boleto_url")

        if not live:
            return PaymentLinkResult(cpf, link=None, dry_run=True,
                                     would_generate=bool(boleto_url))
        if not boleto_url:
            log.warning("sem URL de segunda via para apólice %s", cpf)
            return PaymentLinkResult(cpf, link=None, dry_run=False, would_generate=False)

        # Navega direto à página de emissão (evita lidar com popup window).
        self.session.goto(boleto_url)
        wait_ready(self.page, self.selectors)

        # Seleciona a primeira parcela (mais antiga).
        try:
            first_radio = self.page.locator('input[name*="RBT_Selecionado"]').first
            first_radio.check(timeout=5000)
        except (PlaywrightTimeout, PlaywrightError) as e:
            log.warning("radio não selecionado: %s", e)

        # BTN_Imprimir abre o boleto numa nova janela (popup) com ModalGenerica.aspx.
        # Dentro dela, um iframe mostra ExibeRelatorio.aspx como PDF no viewer do Chrome.
        # Estratégia: detectar popup via context.on("page"), esperar o PDF carregar,
        # clicar no botão de download (seta) do Chrome PDF viewer.
        link: str | None = None
        popup_ref: list = []

        def _on_popup(new_page) -> None:
            popup_ref.append(new_page)

        ctx = self.page.context
        ctx.on("page", _on_popup)
        try:
            self.page.locator('input[name="BTN_Imprimir"]').click(
                timeout=8000, no_wait_after=True
            )
            # Aguarda popup aparecer (até 15 s).
            for _ in range(30):
                if popup_ref:
                    break
                self.page.wait_for_timeout(500)
        except (PlaywrightTimeout, PlaywrightError) as e:
            log.warning("erro ao clicar Imprimir: %s", e)
        finally:
            ctx.remove_listener("page", _on_popup)

        if not popup_ref:
            log.warning("popup não detectado (apólice %s)", cpf)
        else:
            popup = popup_ref[-1]
            try:
                # Espera o PDF carregar completamente no viewer.
                popup.wait_for_load_state("networkidle", timeout=20000)
                popup.wait_for_timeout(3000)

                dest = self._boleto_dir() / f"{cpf}.pdf"
                dest.parent.mkdir(parents=True, exist_ok=True)

                # O botão de download é <cr-icon-button id="save"> dentro do
                # shadow DOM do Chrome PDF viewer. Usamos JS para percorrer
                # os shadow roots e clicar nele.
                # Seletores específicos para o botão de download LOCAL
                # (não o "Salvar no Google Drive" que também tem id="save").
                # iron-icon="cr:file-download" é exclusivo do download para disco.
                _JS_HAS_BTN = """
                    () => {
                        function find(root) {
                            if (!root) return false;
                            if (root.querySelector('cr-icon-button[iron-icon="cr:file-download"]')) return true;
                            if (root.querySelector('cr-icon-button[aria-label="Baixar"]')) return true;
                            for (const n of root.querySelectorAll('*')) {
                                if (n.shadowRoot && find(n.shadowRoot)) return true;
                            }
                            return false;
                        }
                        return find(document);
                    }
                """
                _JS_CLICK_SAVE = """
                    () => {
                        function findInShadow(root) {
                            if (!root) return null;
                            let el = root.querySelector('cr-icon-button[iron-icon="cr:file-download"]');
                            if (el) return el;
                            el = root.querySelector('cr-icon-button[aria-label="Baixar"]');
                            if (el) return el;
                            for (const n of root.querySelectorAll('*')) {
                                if (n.shadowRoot) {
                                    const found = findInShadow(n.shadowRoot);
                                    if (found) return found;
                                }
                            }
                            return null;
                        }
                        const btn = findInShadow(document);
                        if (btn) { btn.click(); return true; }
                        return false;
                    }
                """
                downloaded = False
                all_frames = [popup] + list(popup.frames)
                log.debug("frames disponíveis: %s", [f.url for f in popup.frames])

                for target in all_frames:
                    try:
                        has_btn = target.evaluate(_JS_HAS_BTN)
                        if not has_btn:
                            log.debug("sem botão em: %s", getattr(target, "url", "?")[:60])
                            continue
                        log.info("botão encontrado em frame: %s", getattr(target, "url", "?")[:60])
                        with popup.expect_download(timeout=10000) as dl_info:
                            target.evaluate(_JS_CLICK_SAVE)
                        dl_info.value.save_as(str(dest))
                        downloaded = True
                        log.info("boleto baixado via JS shadow DOM: %s", dest)
                        break
                    except Exception as ex:
                        log.debug("frame %s: %s", getattr(target, "url", "popup")[:60], ex)

                if not downloaded:
                    # Fallback: baixar diretamente da URL do iframe com cookies de sessão.
                    saved = self._save_popup_pdf(popup, cpf)
                    if saved:
                        downloaded = True
                        dest = saved

                if downloaded:
                    link = str(dest)
                else:
                    log.warning("PDF não capturado (apólice %s)", cpf)
                    link = popup.url if popup.url not in ("about:blank", "") else None
            except (PlaywrightTimeout, PlaywrightError) as e:
                log.warning("erro aguardando popup carregar: %s", e)
                link = popup_ref[-1].url if popup_ref else None

        log.info("segunda via gerada: apólice=%s link=%s", cpf, link or "(sem URL)")
        return PaymentLinkResult(cpf, link=link, dry_run=False,
                                 would_generate=True, generated_at=now_utc())

    # --- helpers de PDF ------------------------------------------------------

    def _boleto_dir(self) -> pathlib.Path:
        return pathlib.Path(self.cfg.db_path).parent / "boletos"

    def _save_popup_pdf(self, popup, cpf: str) -> pathlib.Path | None:
        """Baixa o PDF do boleto diretamente da URL do iframe (ExibeRelatorio.aspx).

        ExibeRelatorio.aspx retorna o PDF como application/pdf — não precisa de
        printToPDF. Usamos page.context.request para fazer o GET com as cookies
        de sessão ativas.
        """
        try:
            # Extrai a URL do iframe com o boleto (ExibeRelatorio.aspx).
            iframe_url = popup.evaluate("""
                () => {
                    const iframe = document.querySelector('iframe[src]');
                    return iframe ? iframe.src : null;
                }
            """)
            if not iframe_url:
                # Tenta achar nos frames registrados pelo Playwright.
                for frame in popup.frames:
                    if frame.url and "ExibeRelatorio" in frame.url:
                        iframe_url = frame.url
                        break

            if not iframe_url:
                log.warning("iframe do boleto não encontrado em %s", popup.url)
                return None

            log.debug("baixando PDF do iframe: %s", iframe_url)
            response = popup.context.request.get(iframe_url)
            if not response.ok:
                log.warning("GET iframe retornou %s", response.status)
                return None

            pdf_bytes = response.body()
            if not pdf_bytes.startswith(b"%PDF"):
                log.warning(
                    "GET iframe não retornou PDF (%d bytes, inicio=%r): %s",
                    len(pdf_bytes), pdf_bytes[:8], iframe_url[:80]
                )
                return None
            dest = self._boleto_dir() / f"{cpf}.pdf"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(pdf_bytes)
            log.info("boleto baixado via request direto: %s (%d bytes)", dest, len(pdf_bytes))
            return dest
        except Exception as e:
            log.warning("download do PDF falhou: %s", e)
            return None

    # --- re-check de status --------------------------------------------------

    def check_status(self, cpf: str) -> ClientStatus:
        cpf = normalize_cpf(cpf)
        cents = self.check_client_inadimplente_cents(cpf)
        all_reg = cents == 0
        comp = CompetenciaStatus(
            competencia="resumo",
            situacao=Situacao.REGULARIZADA if all_reg else Situacao.EM_ABERTO,
            valor_cents=cents if cents else None,
        )
        return ClientStatus(cpf, competencias=(comp,), all_regularized=all_reg,
                            checked_at=now_utc())

    def check_client_inadimplente_cents(self, cpf: str) -> int | None:
        """Sinal de pagamento: sumiu do Relatório de Atraso => 0 (pagou).

        Espelha o método homônimo da MAG (usado pelo agente inbound 'já paguei',
        que NUNCA confia no texto do cliente).
        """
        cpf = normalize_cpf(cpf)
        try:
            self.discover_delinquents()
        except ConnectorError:
            return None
        row = self._last_rows.get(cpf)
        if row is None:
            return 0  # não está mais em atraso => regularizou
        return parse_brl_to_cents(row.get("valor"))


__all__ = ["PrudentialConnector"]
