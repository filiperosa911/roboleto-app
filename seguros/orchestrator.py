"""Orquestrador — o loop diário de 5 passos.

1. Login        2. Descoberta (dia 0: enroll + WhatsApp)
3. Follow-up (dia 2: e-mail)   4. Reconciliação   5. Log/relatório

Invariantes:
- ``*_enviado_em`` é gravado SÓ após envio real confirmado (live) -> dry-run
  nunca consome o envio e um crash no meio não duplica.
- fronteira de erro por cliente: um cliente falho não aborta o run.
- na dúvida, NÃO envia (os gates em ``domain.state`` decidem).
"""

from __future__ import annotations

import logging

from .clock import iso_utc, now_in, within_send_window
from .config import Config
from .connectors.base import Delinquent, SeguradoraConnector
from .connectors.factory import insurer_has_payment_link
from .cpf import normalize_cpf
from .domain.models import Canal, ClienteRegua, Modo, ReguaStatus, Resultado
from .domain.state import evaluate, evaluate_email
from .messaging.email import SmtpAuthError
from .messaging.phone import canonical_brazilian_phone, is_plausible_email, is_valid_whatsapp
from .messaging.templates import (
    EMAIL_DIA2_ASSUNTO,
    EMAIL_DIA2_ASSUNTO_LEMBRETE,
    EMAIL_DIA2_HTML,
    EMAIL_DIA2_HTML_LEMBRETE,
    EMAIL_DIA2_TEXTO,
    EMAIL_DIA2_TEXTO_LEMBRETE,
    WHATSAPP_DIA0,
    WHATSAPP_DIA0_LEMBRETE,
    brl_from_cents,
    primeiro_nome,
    render,
)
from .messaging.whatsapp import ZApiNotConnected
from .report import ReportRow, RunReport

log = logging.getLogger("seguros.orchestrator")

PLACEHOLDER_LINK = "https://pagamento.mag.com.br/[seria-gerado-em-modo-live]"


class CircuitBreaker(Exception):
    """Limite global de envios por run excedido — aborta para evitar runaway."""


class Orchestrator:
    def __init__(
        self,
        *,
        config: Config,
        connector: SeguradoraConnector,
        repo,
        optout_repo,
        log_repo,
        wa_sender,
        email_sender,
        report: RunReport,
        notifier=None,
        limit: int | None = None,
    ) -> None:
        self.cfg = config
        self.connector = connector
        self.repo = repo
        self.optout = optout_repo
        self.log_repo = log_repo
        self.wa = wa_sender
        self.email = email_sender
        self.report = report
        self.notifier = notifier
        self.limit = limit
        self.modo = Modo.LIVE if config.live else Modo.DRY_RUN
        # Seguradora sem link (Prudential): régua = LEMBRETE (templates sem link,
        # gate não exige link). Fonte única: capability por seguradora.
        self._requer_link = insurer_has_payment_link(config.insurer)

        self._sends = 0
        self._wa_passed = 0  # contam para o teto diário de WhatsApp
        self._wa_consec_fail = 0
        self._wa_disabled = False
        self._email_disabled = False

    # --- loop principal ------------------------------------------------------

    def run(self) -> RunReport:
        cfg = self.cfg
        log.info("início modo=%s corretor=%s", self.modo.value, cfg.corretor_id)
        self.connector.ensure_authenticated(interactive=True)

        now_local = now_in(cfg.timezone)
        window_open = within_send_window(
            now_local, cfg.horario_inicio, cfg.horario_fim, cfg.dias_uteis_apenas
        )
        if not window_open:
            log.info("fora da janela de envio (%s); envios serão adiados", now_local.strftime("%a %H:%M"))

        if cfg.live:
            try:
                self.wa.healthcheck()
            except ZApiNotConnected as err:
                log.error("WhatsApp indisponível: %s — pulando envios de WhatsApp", err)
                self._wa_disabled = True

        # --- passo 2a: retoma WhatsApp pendente (dia 0 adiado/erro em runs anteriores) ---
        for cliente in self.repo.pending_whatsapp():
            try:
                self._dispatch_whatsapp(cliente, window_open, would_generate=True)
            except CircuitBreaker:
                raise
            except Exception as err:  # noqa: BLE001
                log.exception("falha no WhatsApp pendente cpf=%s: %s", cliente.cpf, err)

        # --- passo 2b: descoberta ---
        delinquents = self.connector.discover_delinquents()
        log.info("descoberta: %d inadimplente(s)", len(delinquents))
        processados = 0
        for d in delinquents:
            if self.limit is not None and processados >= self.limit:
                log.info("limite --limit=%d atingido na descoberta", self.limit)
                break
            try:
                self._process_new(d, window_open)
            except CircuitBreaker:
                raise
            except Exception as err:  # noqa: BLE001 - fronteira por cliente
                log.exception("falha processando inadimplente cpf=%s: %s", normalize_cpf(d.cpf), err)
            processados += 1

        # --- passo 3: follow-up (dia 2) ---
        for cliente in self.repo.due_for_followup(cfg.followup_offset_days):
            try:
                self._process_followup(cliente, window_open)
            except CircuitBreaker:
                raise
            except Exception as err:  # noqa: BLE001
                log.exception("falha no follow-up cpf=%s: %s", cliente.cpf, err)

        # --- passo 4: reconciliação ---
        if delinquents:
            current = {normalize_cpf(d.cpf) for d in delinquents}
            for cpf in self.repo.active_cpfs():
                if cpf in current:
                    continue
                try:
                    if self.connector.check_status(cpf).all_regularized:
                        if self.cfg.live:  # dry-run não persiste mutação de estado
                            self.repo.mark_resolved(cpf)
                        self.log_repo.record(
                            cpf=cpf, canal=Canal.SISTEMA, resultado=Resultado.REGULARIZADO,
                            modo=self.modo, payload_resumo="reconciliado: saiu da inadimplência",
                        )
                except Exception as err:  # noqa: BLE001
                    log.exception("falha na reconciliação cpf=%s: %s", cpf, err)
        else:
            log.warning("descoberta vazia; reconciliação pulada nesta execução")

        log.info("fim modo=%s envios=%d", self.modo.value, self._sends)
        return self.report

    # --- passo 2: novo inadimplente -> enroll + WhatsApp dia 0 ---------------

    def _process_new(self, d: Delinquent, window_open: bool) -> None:
        cpf = normalize_cpf(d.cpf)
        if self.optout.is_opted_out(cpf=cpf):
            self.log_repo.record(cpf=cpf, canal=Canal.SISTEMA, resultado=Resultado.PULADO_OPTOUT,
                                 modo=self.modo, payload_resumo="opt-out: não contatado")
            self.report.add(
                ReportRow(
                    cpf=cpf, nome=d.nome, primeiro_nome=primeiro_nome(d.nome), canal="sistema",
                    dia_regua=0, decisao=Resultado.PULADO_OPTOUT.value, destino="",
                    competencia=d.competencia or "", valor_total=brl_from_cents(d.valor_total_cents),
                    link_pagamento="", mensagem_renderizada="", detalhe="opt-out",
                )
            )
            return
        if self.repo.exists(cpf):
            return  # já em régua (idempotência via DB)

        # Ordem otimizada: generate_payment_link abre o detalhe da inadimplência e
        # cacheia o link do perfil; fetch_contact reusa esse cache (1 abertura, não 2).
        link_result = self.connector.generate_payment_link(cpf, live=self.cfg.live)
        contact = self.connector.fetch_contact(cpf)

        telefone = contact.celular or contact.telefone
        cliente = ClienteRegua(
            cpf=cpf,
            corretor_id=self.cfg.corretor_id,
            nome=d.nome,
            telefone=telefone,
            email=contact.email,
            valor_inadimplente_cents=d.valor_total_cents,
            valor_texto=d.valor_texto,
            vencimento_mais_antigo=d.vencimento_mais_antigo,
            competencia=d.competencia,
            work_status=d.status.value if d.status else None,
            link_pagamento=link_result.link,
            link_gerado_em=iso_utc() if link_result.link else None,
            autoriza_whatsapp=contact.autoriza_whatsapp,
            autoriza_email=contact.autoriza_email,
            enrolled_em=iso_utc(),
            status=ReguaStatus.EM_REGUA,
        )
        # DRY-RUN é só preview: NÃO persiste enrollment (senão o exists() faria o
        # run --live seguinte pular o cliente e nunca gerar link/enviar).
        if self.cfg.live:
            self.repo.insert_enrollment(cliente)
        self._dispatch_whatsapp(cliente, window_open, would_generate=link_result.would_generate)

    # --- passo 3: follow-up -> e-mail dia 2 ---------------------------------

    def _process_followup(self, cliente: ClienteRegua, window_open: bool) -> None:
        status = self.connector.check_status(cliente.cpf)
        if status.all_regularized:
            if self.cfg.live:  # dry-run não persiste mutação de estado
                self.repo.mark_resolved(cliente.cpf)
            self.log_repo.record(cpf=cliente.cpf, canal=Canal.SISTEMA,
                                 resultado=Resultado.REGULARIZADO, modo=self.modo,
                                 payload_resumo="regularizado no follow-up")
            return
        self._dispatch_email(cliente, window_open)

    # --- dispatch WhatsApp (dia 0) ------------------------------------------

    def _dispatch_whatsapp(self, cliente: ClienteRegua, window_open: bool, *,
                           would_generate: bool) -> None:
        override = self.cfg.whatsapp_override_to
        opted_out = self.optout.is_opted_out(
            cpf=cliente.cpf, telefone=canonical_brazilian_phone(cliente.telefone)
        )
        if self.cfg.live:
            tem_link = bool(cliente.link_pagamento)
            link_efetivo = cliente.link_pagamento or ""
        else:
            tem_link = would_generate or bool(cliente.link_pagamento)
            link_efetivo = cliente.link_pagamento or PLACEHOLDER_LINK

        # MODO TESTE (override): valida o telefone do DESTINO (o próprio número).
        destino_valido = is_valid_whatsapp(override) if override else is_valid_whatsapp(cliente.telefone)

        template = WHATSAPP_DIA0 if self._requer_link else WHATSAPP_DIA0_LEMBRETE
        ctx = self._ctx(cliente, link_efetivo)
        mensagem = render(template, ctx)
        decision = evaluate(
            Canal.WHATSAPP, opted_out=opted_out, tem_link=tem_link,
            destino_valido=destino_valido,
            ja_enviado=cliente.whatsapp_enviado_em is not None, window_open=window_open,
            requer_link=self._requer_link,
        )

        if not decision.should_send:
            self._record(cliente, Canal.WHATSAPP, 0, decision.resultado, cliente.telefone or "",
                         link_efetivo, mensagem)
            return
        if self._wa_disabled:
            self._record(cliente, Canal.WHATSAPP, 0, Resultado.PULADO_LOTE_ABORTADO,
                         cliente.telefone or "", link_efetivo, mensagem)
            return
        if self._wa_passed >= self.cfg.max_whatsapp_por_dia:
            self._record(cliente, Canal.WHATSAPP, 0, Resultado.PULADO_LIMITE_DIARIO,
                         cliente.telefone or "", link_efetivo, mensagem)
            return
        self._check_circuit_breaker()
        self._wa_passed += 1

        # FASE DE TESTE: redireciona o destino para o número do corretor.
        destino_envio = override or cliente.telefone
        nota = f"[TESTE→{override}] cliente real: {cliente.telefone}" if override else ""

        result = self.wa.send(destino_envio, mensagem)
        if result.sent:  # só em live, sucesso real
            self.repo.mark_whatsapp_sent(cliente.cpf)
            self._wa_consec_fail = 0
        elif result.resultado is Resultado.ERRO:
            self._wa_consec_fail += 1
            if self._wa_consec_fail >= self.cfg.max_falhas_consecutivas:
                log.error("%d falhas consecutivas de WhatsApp — abortando lote",
                          self._wa_consec_fail)
                self._wa_disabled = True
        self._sends += 1
        detalhe = " ".join(x for x in (nota, result.detail or (result.message_id or "")) if x)
        self._record(cliente, Canal.WHATSAPP, 0, result.resultado, destino_envio or "",
                     link_efetivo, mensagem, detalhe=detalhe)

    # --- dispatch e-mail (dia 2) --------------------------------------------

    def _dispatch_email(self, cliente: ClienteRegua, window_open: bool) -> None:
        email_valido = is_plausible_email(cliente.email)
        opted_out = self.optout.is_opted_out(cpf=cliente.cpf)
        if self.cfg.live:
            tem_link = bool(cliente.link_pagamento)
            link_efetivo = cliente.link_pagamento or ""
        else:
            tem_link = True  # em dry-run o link já teria sido gerado no dia 0
            link_efetivo = cliente.link_pagamento or PLACEHOLDER_LINK

        assunto_t = EMAIL_DIA2_ASSUNTO if self._requer_link else EMAIL_DIA2_ASSUNTO_LEMBRETE
        texto_t = EMAIL_DIA2_TEXTO if self._requer_link else EMAIL_DIA2_TEXTO_LEMBRETE
        html_t = EMAIL_DIA2_HTML if self._requer_link else EMAIL_DIA2_HTML_LEMBRETE
        ctx = self._ctx(cliente, link_efetivo)
        assunto = render(assunto_t, ctx)
        texto = render(texto_t, ctx)
        html = render(html_t, ctx, escape_html=True)
        decision = evaluate_email(
            cliente, opted_out=opted_out, email_valido=email_valido,
            tem_link=tem_link, window_open=window_open,
            requer_link=self._requer_link,
        )

        if not decision.should_send:
            self._record(cliente, Canal.EMAIL, 2, decision.resultado, cliente.email or "",
                         link_efetivo, texto)
            return
        if self._email_disabled:
            self._record(cliente, Canal.EMAIL, 2, Resultado.PULADO_LOTE_ABORTADO,
                         cliente.email or "", link_efetivo, texto)
            return
        self._check_circuit_breaker()

        try:
            result = self.email.send(cliente.email, assunto, texto, html)
        except SmtpAuthError as err:
            log.error("autenticação de e-mail falhou — abortando lote de e-mail: %s", err)
            self._email_disabled = True
            self._record(cliente, Canal.EMAIL, 2, Resultado.ERRO, cliente.email or "",
                         link_efetivo, texto, detalhe=str(err))
            return
        if result.sent:
            self.repo.mark_email_sent(cliente.cpf)
        self._sends += 1
        self._record(cliente, Canal.EMAIL, 2, result.resultado, cliente.email or "",
                     link_efetivo, texto, detalhe=result.detail or "")

    # --- helpers -------------------------------------------------------------

    def _ctx(self, cliente: ClienteRegua, link_efetivo: str) -> dict:
        return {
            "primeiro_nome": primeiro_nome(cliente.nome),
            "competencia": cliente.competencia or "—",
            "valor_total": brl_from_cents(cliente.valor_inadimplente_cents),
            "link_pagamento": link_efetivo,
            "nome_corretor": self.cfg.nome_corretor,
            "corretora": self.cfg.nome_corretora,
        }

    def _record(self, cliente: ClienteRegua, canal: Canal, dia: int, resultado: Resultado,
                destino: str, link: str, mensagem: str, detalhe: str = "") -> None:
        self.log_repo.record(cpf=cliente.cpf, canal=canal, resultado=resultado, modo=self.modo,
                             link=link, payload_resumo=detalhe or resultado.value)
        self.report.add(
            ReportRow(
                cpf=cliente.cpf,
                nome=cliente.nome,
                primeiro_nome=primeiro_nome(cliente.nome),
                canal=canal.value,
                dia_regua=dia,
                decisao=resultado.value,
                destino=destino,
                competencia=cliente.competencia or "",
                valor_total=brl_from_cents(cliente.valor_inadimplente_cents),
                link_pagamento=link,
                mensagem_renderizada=mensagem,
                detalhe=detalhe,
            )
        )

    def _check_circuit_breaker(self) -> None:
        if self._sends >= self.cfg.max_sends_per_run:
            raise CircuitBreaker(
                f"MAX_SENDS_PER_RUN={self.cfg.max_sends_per_run} excedido"
            )


__all__ = ["Orchestrator", "CircuitBreaker", "PLACEHOLDER_LINK"]
