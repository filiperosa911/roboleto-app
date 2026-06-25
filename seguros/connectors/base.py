"""Fronteira de abstração da seguradora — a ÚNICA do projeto.

``SeguradoraConnector`` é a interface que o orquestrador conhece. Hoje há um
único impl (``MagConnector``); futura multi-seguradora pluga aqui sem outra
abstração. Os DTOs abaixo são o vocabulário trocado com o resto do app — nada
acima do conector importa nada de ``connectors.mag``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class WorkStatus(str, Enum):
    NAO_TRABALHADO = "naoTrabalhado"
    TRABALHADO_PARCIALMENTE = "trabalhadoParcialmente"
    TRABALHADO = "trabalhado"
    UNKNOWN = "unknown"


class Situacao(str, Enum):
    REGULARIZADA = "regularizada"
    EM_ABERTO = "em_aberto"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Delinquent:
    """Uma linha da página de Inadimplências."""

    cpf: str  # normalizado, 11 dígitos
    nome: str
    vencimento_mais_antigo: str | None = None  # ISO date YYYY-MM-DD
    valor_total_cents: int | None = None
    valor_texto: str | None = None
    competencia: str | None = None
    telefone: str | None = None  # preenchido quando vem na mesma grade (ex.: Prudential)
    status: WorkStatus = WorkStatus.UNKNOWN
    raw: dict = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class Contact:
    """Contato + flags de consentimento, da página Meus Clientes (VI)."""

    cpf: str
    email: str | None = None
    celular: str | None = None
    telefone: str | None = None
    autoriza_whatsapp: bool = False
    autoriza_email: bool = False
    autoriza_sms: bool = False
    found: bool = True


@dataclass(frozen=True)
class CompetenciaStatus:
    competencia: str
    situacao: Situacao
    valor_cents: int | None = None


@dataclass(frozen=True)
class ClientStatus:
    cpf: str
    competencias: tuple[CompetenciaStatus, ...]
    all_regularized: bool
    checked_at: datetime


@dataclass(frozen=True)
class PaymentLinkResult:
    cpf: str
    link: str | None  # None em dry-run ou se não capturado
    dry_run: bool
    would_generate: bool = False  # dry-run: localizou "Cobrar" e geraria o link
    already_worked: bool = False  # competências já estavam em "Trabalhadas"
    generated_at: datetime | None = None


# --- exceções ----------------------------------------------------------------


class ConnectorError(Exception):
    """Erro base de conector."""


class NotAuthenticatedError(ConnectorError):
    """Sessão expirada e não foi possível re-autenticar."""


class SessionExpiredError(ConnectorError):
    """Sessão caiu no meio do run."""


class ClientNotFoundError(ConnectorError):
    """CPF não encontrado em Meus Clientes."""


class PaymentLinkNotCapturedError(ConnectorError):
    """Link gerado mas não foi possível capturá-lo."""


# --- interface ---------------------------------------------------------------


class SeguradoraConnector(ABC):
    name: str = "base"

    def __enter__(self) -> "SeguradoraConnector":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def start(self) -> None:  # pragma: no cover - opcional
        pass

    def close(self) -> None:  # pragma: no cover - opcional
        pass

    @abstractmethod
    def ensure_authenticated(self, *, interactive: bool) -> None:
        """Garante sessão válida; se morta e ``interactive``, pausa p/ login humano."""

    @abstractmethod
    def discover_delinquents(self) -> list[Delinquent]:
        """Lista os inadimplentes (paginando até o fim)."""

    @abstractmethod
    def fetch_contact(self, cpf: str) -> Contact:
        """Contato + consentimento de um CPF (Meus Clientes VI)."""

    @abstractmethod
    def generate_payment_link(self, cpf: str, *, live: bool) -> PaymentLinkResult:
        """Gera o link consolidado. Em ``live=False`` NÃO clica "Cobrar" (sem mutação)."""

    @abstractmethod
    def check_status(self, cpf: str) -> ClientStatus:
        """Situação das competências (Regularizada vs Em aberto)."""


__all__ = [
    "WorkStatus",
    "Situacao",
    "Delinquent",
    "Contact",
    "CompetenciaStatus",
    "ClientStatus",
    "PaymentLinkResult",
    "ConnectorError",
    "NotAuthenticatedError",
    "SessionExpiredError",
    "ClientNotFoundError",
    "PaymentLinkNotCapturedError",
    "SeguradoraConnector",
]
