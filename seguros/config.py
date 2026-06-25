"""Carga e validação de configuração (a partir do ``.env``).

Falha rápido e lista TODAS as variáveis faltantes/ inválidas de uma vez, em vez
de estourar na primeira. Em modo ``--live`` exige as credenciais de envio.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from .clock import parse_hhmm


class ConfigError(Exception):
    """Configuração ausente ou inválida."""


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "sim", "y", "s"}


def _as_int(value: str | None, default: int) -> int:
    if value is None or value.strip() == "":
        return default
    try:
        return int(value.strip())
    except ValueError as exc:  # pragma: no cover - validado em load_config
        raise ConfigError(f"valor inteiro inválido: {value!r}") from exc


def _parse_hora(value: str | None, default: time) -> time:
    if value is None or value.strip() == "":
        return default
    try:
        return parse_hhmm(value)
    except (ValueError, IndexError):
        return default


@dataclass(frozen=True)
class Config:
    # modo (vem da CLI, não do .env)
    live: bool

    # identidade / multi-tenant
    corretor_id: str
    nome_corretor: str
    nome_corretora: str
    dashboard_password: str  # senha do painel (vazio = login provisório, qualquer senha)

    # caminhos / sessão
    db_path: Path
    user_data_dir: Path

    # URLs MAG
    mag_login_url: str
    mag_inadimplencias_url: str
    mag_clientes_url: str

    # Z-API
    zapi_instance_id: str
    zapi_token: str
    zapi_client_token: str
    notify_whatsapp_to: str
    # FASE DE TESTE: se preenchido, TODO WhatsApp vai para este número (o do
    # corretor), NUNCA para o do cliente. Esvazie para enviar de verdade aos clientes.
    whatsapp_override_to: str

    # agente inbound (webhook Z-API)
    admin_whatsapp: str  # número do admin p/ avisos (vazio = cai no notify_whatsapp_to)
    zapi_webhook_secret: str  # segredo no path do webhook; vazio = webhook REJEITA tudo
    usar_llm_intent: bool  # liga o seam LLM p/ frases ambíguas (precisa anthropic_api_key)
    anthropic_api_key: str
    reschedule_max_dias: int  # teto de dias p/ a data desejada de remarcação

    # Gmail
    gmail_address: str
    gmail_app_password: str

    # janela / cadência
    timezone: str
    horario_inicio: time
    horario_fim: time
    dias_uteis_apenas: bool
    followup_offset_days: int

    # revisão matinal do DOM (health-check de seletores)
    healthcheck_auto: bool
    healthcheck_hora: time

    # anti-ban / disjuntores
    max_whatsapp_por_dia: int
    pacing_min_s: int
    pacing_max_s: int
    max_sends_per_run: int
    max_falhas_consecutivas: int

    # limitação conhecida
    payment_link_ttl_days: int | None

    # seguradora ativa ('mag' | 'prudential'); o dashboard pode hospedar mais de
    # uma e seleciona na entrada. Default 'mag' (compatível com o comportamento atual).
    insurer: str = "mag"

    # Prudential (Life Planner AEM + relatório ASPX em saa.prudential.com.br).
    # Defaults conhecidos da exploração; o .env pode sobrescrever. O DOM do
    # formulário/grade está num iframe cross-origin e é calibrado AO VIVO.
    prudential_login_url: str = "https://lifeplanner.prudential.com.br/"
    prudential_home_url: str = "https://lifeplanner.prudential.com.br/"
    prudential_atraso_url: str = (
        "https://saa.prudential.com.br/DBClient/PAG_DBClient_ApoliceAtraso.aspx"
        "?AEMHost=https://lponline.prudential.com.br"
    )
    prudential_dias_atraso_min: int = 1


def load_config(*, live: bool, env_path: str | None = None) -> Config:
    """Lê o ``.env`` (se presente) e o ambiente, validando o resultado.

    ``live=True`` torna obrigatórias as credenciais de Z-API e Gmail.
    """
    if env_path:
        load_dotenv(env_path)
    else:
        load_dotenv()  # procura .env a partir do cwd

    errors: list[str] = []

    # URLs da MAG têm defaults conhecidos (do brief); o .env pode sobrescrever.
    # Inclui TODOS os status (naoTrabalhado + parcial + trabalhado) para popular a
    # base completa de inadimplentes na aplicação.
    _DEFAULT_INADIMPLENCIAS = (
        "https://plataformadosprodutores.mag.com.br/s/inadimplencias"
        "?orderBy=Inadimplencia_Data_Vencimento__c&typeOrderBy=DESC"
        "&cliente=naoTrabalhado&cliente=trabalhadoParcialmente&cliente=trabalhado&pageSize=100"
    )
    _DEFAULT_CLIENTES = (
        "https://plataformadosprodutores.mag.com.br/s/clientes"
        "?status=ativo&tipoCliente=VI&page=1&pageSize=10&orderBy=premioVI&typeOrderBy=DESC"
    )
    mag_login_url = os.environ.get(
        "MAG_LOGIN_URL", "https://plataformadosprodutores.mag.com.br/s/login/"
    ).strip()
    mag_inadimplencias_url = (
        os.environ.get("MAG_INADIMPLENCIAS_URL", "").strip() or _DEFAULT_INADIMPLENCIAS
    )
    mag_clientes_url = os.environ.get("MAG_CLIENTES_URL", "").strip() or _DEFAULT_CLIENTES

    # Credenciais: obrigatórias em --live; em dry-run podem faltar (não enviamos).
    zapi_instance_id = os.environ.get("ZAPI_INSTANCE_ID", "").strip()
    zapi_token = os.environ.get("ZAPI_TOKEN", "").strip()
    zapi_client_token = os.environ.get("ZAPI_CLIENT_TOKEN", "").strip()
    gmail_address = os.environ.get("GMAIL_ADDRESS", "").strip()
    gmail_app_password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()

    if live:
        # ZAPI_CLIENT_TOKEN é opcional (só se a conta tiver token de segurança).
        # Gmail é opcional: sem ele, o canal de e-mail (dia 2) fica desligado.
        for name, val in [
            ("ZAPI_INSTANCE_ID", zapi_instance_id),
            ("ZAPI_TOKEN", zapi_token),
        ]:
            if not val:
                errors.append(f"  - {name} é obrigatória em modo --live")

    nome_corretor = os.environ.get("NOME_CORRETOR", "").strip() or "Sua Corretora"
    nome_corretora = os.environ.get("NOME_CORRETORA", "").strip() or "Corretora"

    try:
        horario_inicio = parse_hhmm(os.environ.get("HORARIO_INICIO", "09:00"))
        horario_fim = parse_hhmm(os.environ.get("HORARIO_FIM", "18:00"))
    except (ValueError, IndexError):
        errors.append("  - HORARIO_INICIO/HORARIO_FIM devem estar no formato HH:MM")
        horario_inicio = time(9, 0)
        horario_fim = time(18, 0)

    if horario_inicio >= horario_fim:
        errors.append("  - HORARIO_INICIO deve ser menor que HORARIO_FIM")

    pacing_min = _as_int(os.environ.get("PACING_MIN_S"), 20)
    pacing_max = _as_int(os.environ.get("PACING_MAX_S"), 45)
    if pacing_min > pacing_max:
        errors.append("  - PACING_MIN_S não pode ser maior que PACING_MAX_S")

    ttl_raw = os.environ.get("PAYMENT_LINK_TTL_DAYS", "").strip()
    payment_link_ttl_days: int | None = None
    if ttl_raw:
        try:
            payment_link_ttl_days = int(ttl_raw)
        except ValueError:
            errors.append("  - PAYMENT_LINK_TTL_DAYS deve ser um número inteiro (dias)")

    timezone = os.environ.get("TIMEZONE", "America/Sao_Paulo").strip()
    try:
        ZoneInfo(timezone)
    except Exception:  # noqa: BLE001 - ZoneInfoNotFoundError e afins
        errors.append(f"  - TIMEZONE inválido: {timezone!r}")

    if errors:
        raise ConfigError(
            "Configuração inválida:\n" + "\n".join(errors) + "\n\nVeja .env.example."
        )

    return Config(
        live=live,
        corretor_id=os.environ.get("CORRETOR_ID", "local").strip() or "local",
        nome_corretor=nome_corretor,
        nome_corretora=nome_corretora,
        dashboard_password=os.environ.get("DASHBOARD_PASSWORD", "").strip(),
        db_path=Path(os.environ.get("DB_PATH", "./regua.sqlite").strip()),
        user_data_dir=Path(
            os.environ.get("PLAYWRIGHT_USER_DATA_DIR", "./.mag_session").strip()
        ),
        mag_login_url=mag_login_url,
        mag_inadimplencias_url=mag_inadimplencias_url,
        mag_clientes_url=mag_clientes_url,
        zapi_instance_id=zapi_instance_id,
        zapi_token=zapi_token,
        zapi_client_token=zapi_client_token,
        notify_whatsapp_to=os.environ.get("NOTIFY_WHATSAPP_TO", "").strip(),
        # FASE DE TESTE: se setado no .env, redireciona todo WhatsApp para este
        # número (o do corretor). Vazio = envia ao cliente real (produção).
        whatsapp_override_to=os.environ.get("WHATSAPP_OVERRIDE_TO", "").strip(),
        admin_whatsapp=os.environ.get("ADMIN_WHATSAPP", "").strip(),
        zapi_webhook_secret=os.environ.get("ZAPI_WEBHOOK_SECRET", "").strip(),
        usar_llm_intent=_as_bool(os.environ.get("USAR_LLM_INTENT"), False),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip(),
        reschedule_max_dias=_as_int(os.environ.get("RESCHEDULE_MAX_DIAS"), 30),
        gmail_address=gmail_address,
        gmail_app_password=gmail_app_password,
        timezone=timezone,
        horario_inicio=horario_inicio,
        horario_fim=horario_fim,
        dias_uteis_apenas=_as_bool(os.environ.get("DIAS_UTEIS_APENAS"), True),
        followup_offset_days=_as_int(os.environ.get("FOLLOWUP_OFFSET_DAYS"), 2),
        healthcheck_auto=_as_bool(os.environ.get("HEALTHCHECK_AUTO"), True),
        healthcheck_hora=_parse_hora(os.environ.get("HEALTHCHECK_HORA"), time(8, 0)),
        max_whatsapp_por_dia=_as_int(os.environ.get("MAX_WHATSAPP_POR_DIA"), 70),
        pacing_min_s=pacing_min,
        pacing_max_s=pacing_max,
        max_sends_per_run=_as_int(os.environ.get("MAX_SENDS_PER_RUN"), 200),
        max_falhas_consecutivas=_as_int(os.environ.get("MAX_FALHAS_CONSECUTIVAS"), 5),
        payment_link_ttl_days=payment_link_ttl_days,
        insurer=(os.environ.get("INSURER", "mag").strip().lower() or "mag"),
        prudential_login_url=(
            os.environ.get("PRUDENTIAL_LOGIN_URL", "").strip()
            or "https://lifeplanner.prudential.com.br/"
        ),
        prudential_home_url=(
            os.environ.get("PRUDENTIAL_HOME_URL", "").strip()
            or "https://lifeplanner.prudential.com.br/"
        ),
        prudential_atraso_url=(
            os.environ.get("PRUDENTIAL_ATRASO_URL", "").strip()
            or "https://saa.prudential.com.br/DBClient/PAG_DBClient_ApoliceAtraso.aspx"
            "?AEMHost=https://lponline.prudential.com.br"
        ),
        prudential_dias_atraso_min=_as_int(os.environ.get("PRUDENTIAL_DIAS_ATRASO_MIN"), 1),
    )


def config_for_insurer(base: Config, insurer: str) -> Config:
    """Deriva a config de uma seguradora a partir da ``base``.

    A MAG mantém o escopo/sessão atuais (compatibilidade). As demais ficam
    isoladas em ``corretor_id:insurer`` (mesmo banco, sem migração) e na pasta de
    sessão ``.{insurer}_session``. Usado pelo dashboard (multi-seguradora) e pelo
    CLI (``--insurer``) para que os dois escopem do mesmo jeito.
    """
    insurer = (insurer or "mag").lower()
    if insurer == "mag":
        return replace(base, insurer="mag")
    return replace(
        base,
        insurer=insurer,
        corretor_id=f"{base.corretor_id}:{insurer}",
        user_data_dir=Path(f"./.{insurer}_session"),
        healthcheck_auto=False,
    )


__all__ = ["Config", "ConfigError", "load_config", "config_for_insurer"]
