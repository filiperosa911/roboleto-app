"""Modelo de domínio da régua: enums, a entidade ``ClienteRegua`` e ``Decision``.

DTOs vindos da plataforma (``Delinquent``, ``Contact``, ...) ficam em
``connectors.base`` — aqui mora apenas o estado da régua (o que persiste no SQLite).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ReguaStatus(str, Enum):
    EM_REGUA = "em_regua"
    RESOLVIDO = "resolvido"
    OPT_OUT = "opt_out"


class Canal(str, Enum):
    WHATSAPP = "whatsapp"
    EMAIL = "email"
    SISTEMA = "sistema"


class Modo(str, Enum):
    DRY_RUN = "dry_run"
    LIVE = "live"


class Resultado(str, Enum):
    ENVIADO = "enviado"
    DRY_RUN = "dry_run"
    ERRO = "erro"
    PULADO_CONSENTIMENTO = "pulado_consentimento"
    PULADO_OPTOUT = "pulado_optout"
    PULADO_JANELA = "pulado_janela"
    PULADO_IDEMPOTENTE = "pulado_idempotente"
    PULADO_LIMITE_DIARIO = "pulado_limite_diario"
    PULADO_LOTE_ABORTADO = "pulado_lote_abortado"
    TELEFONE_INVALIDO = "telefone_invalido"
    EMAIL_INVALIDO = "email_invalido"
    SEM_LINK = "sem_link"
    REGULARIZADO = "regularizado"


@dataclass
class ClienteRegua:
    """Uma linha de ``clientes_regua`` — um cliente em régua para um corretor."""

    cpf: str  # 11 dígitos normalizados
    nome: str
    corretor_id: str = "local"
    telefone: str | None = None
    email: str | None = None
    valor_inadimplente_cents: int | None = None
    valor_texto: str | None = None
    vencimento_mais_antigo: str | None = None  # ISO date YYYY-MM-DD
    competencia: str | None = None
    work_status: str | None = None  # status MAG: naoTrabalhado/trabalhadoParcialmente/trabalhado
    link_pagamento: str | None = None
    link_gerado_em: str | None = None
    autoriza_whatsapp: bool = False
    autoriza_email: bool = False
    whatsapp_enviado_em: str | None = None  # ISO UTC ou None
    email_enviado_em: str | None = None
    follow_up_enviado_em: str | None = None  # 2º toque (dia 2)
    primeiro_disparo_em: str | None = None  # 1º toque (WhatsApp ou e-mail)
    resolvido_em: str | None = None  # quando detectamos o pagamento
    tempo_ate_pagar_horas: float | None = None
    conversao_atribuida: bool = False  # pagou DEPOIS do disparo
    valor_recuperado_cents: int | None = None
    ultimo_check_em: str | None = None
    checks_count: int = 0
    enrolled_em: str | None = None  # ISO UTC — "dia 0"
    status: ReguaStatus = ReguaStatus.EM_REGUA
    atualizado_em: str | None = None

    @property
    def primeiro_nome(self) -> str:
        partes = (self.nome or "").strip().split()
        return partes[0].capitalize() if partes else ""


# --- resultado dos gates -----------------------------------------------------


class Acao(str, Enum):
    SEND = "send"
    SKIP = "skip"
    DEFER = "defer"


@dataclass(frozen=True)
class Decision:
    """Resultado da avaliação dos gates para (cliente, canal)."""

    acao: Acao
    resultado: Resultado | None = None  # motivo, quando SKIP/DEFER

    @property
    def should_send(self) -> bool:
        return self.acao is Acao.SEND

    @staticmethod
    def send() -> "Decision":
        return Decision(Acao.SEND)

    @staticmethod
    def skip(resultado: Resultado) -> "Decision":
        return Decision(Acao.SKIP, resultado)

    @staticmethod
    def defer(resultado: Resultado) -> "Decision":
        return Decision(Acao.DEFER, resultado)


__all__ = [
    "ReguaStatus",
    "Canal",
    "Modo",
    "Resultado",
    "ClienteRegua",
    "Acao",
    "Decision",
]
