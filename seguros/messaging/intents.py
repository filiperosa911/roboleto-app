"""Classificador de intenção das respostas do cliente (rule-based, offline).

Determinístico, sem custo e auditável. Precedência (primeiro que casa vence):
SAIR > JA_PAGUEI > RESCHEDULE > NOVO_LINK > SAUDACAO > DUVIDA.

Decisões de segurança (do review adversarial):
- SAIR exige match FORTE (palavra-gatilho em mensagem curta OU frase-âncora) para
  não dar opt-out indevido em frases como "não consigo sair de casa pra pagar".
- JA_PAGUEI casa só o PASSADO ("paguei", "quitei") — nunca o futuro ("pago dia 30"),
  e respeita negação ("ainda não paguei").
- A extração de data faz clamp em [hoje+1 .. hoje+max_dias]; passado/absurdo -> None.

O seam LLM (intent_llm.py) é opcional e nunca decide opt-out/ação financeira sozinho.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta

INTENT_SAIR = "SAIR"
INTENT_JA_PAGUEI = "JA_PAGUEI"
INTENT_RESCHEDULE = "RESCHEDULE"
INTENT_NOVO_LINK = "NOVO_LINK"
INTENT_SAUDACAO = "SAUDACAO"
INTENT_DUVIDA = "DUVIDA"


@dataclass(frozen=True)
class IntentResult:
    intent: str
    confianca: float
    data_desejada: str | None = None  # ISO date YYYY-MM-DD (só no RESCHEDULE)
    matched: str | None = None


def _norm(texto: str | None) -> str:
    """lowercase + remove acentos (NFKD), preservando espaços e dígitos."""
    t = unicodedata.normalize("NFKD", (texto or "").lower())
    return "".join(c for c in t if not unicodedata.combining(c)).strip()


# --- SAIR (precedência máxima) ----------------------------------------------
_SAIR_WORDS = {"sair", "sai", "parar", "pare", "parem", "cancelar", "cancela",
               "descadastrar", "remover", "stop"}
_SAIR_PHRASES = [
    "nao quero mais receber", "nao quero receber", "nao quero mais",
    "me tira da lista", "me tire da lista", "pare de enviar", "parem de enviar",
    "para de enviar", "nao perturbe", "nao me perturbe", "me remova",
    "me descadastr", "nao me mande mais", "nao manda mais", "nao envie mais",
    "nao quero ser incomodad", "quero sair", "quero parar", "quero cancelar",
    "quero me descadastrar", "desejo sair", "gostaria de sair",
]

# --- JA_PAGUEI (passado/concluído) ------------------------------------------
_PAGO_PHRASES = [
    "ja paguei", "ja quitei", "paguei", "quitei", "fiz o pagamento",
    "efetuei o pagamento", "efetuei pagamento", "realizei o pagamento",
    "acabei de pagar", "ta pago", "esta pago", "foi pago", "esta quitado",
    "pagamento realizado", "pagamento efetuado", "pagamento feito", "comprovante",
]
_PAGO_NEG = ["nao paguei", "ainda nao paguei", "ainda nao quitei", "nao quitei",
             "nao consegui pagar", "nao foi pago", "nao paguei ainda"]

# --- RESCHEDULE (adiamento) --------------------------------------------------
_RESCHEDULE_MARKERS = [
    "outro dia", "outra data", "mais pra frente", "mais para frente",
    "adiar", "adia", "remarcar", "remarca", "prorrogar", "prorroga", "prorrogacao",
    "mais prazo", "mais uns dias", "uns dias", "alguns dias", "quando der",
    "nao consigo agora", "nao tenho como agora", "nao da agora", "nao posso agora",
    "me da um tempo", "me de um tempo", "semana que vem", "proxima semana",
    "mes que vem", "proximo mes", "so recebo", "so no dia", "pode ser dia",
    "fim do mes", "final do mes",
]

# --- NOVO_LINK (link/boleto com problema, sem adiamento) --------------------
_LINK_BASE = ("link", "boleto")
_LINK_KEYS = ["nao abre", "nao funciona", "nao carrega", "nao consigo", "expirou",
              "venceu", "vencido", "de novo", "outro", "novo", "reenvia", "reenviar",
              "manda", "gera", "gerar", "erro", "invalido", "quebrad"]

_SAUDACAO = {"oi", "ola", "ola!", "opa", "eai", "bom", "boa", "dia", "tarde", "noite",
             "obrigado", "obrigada", "obg", "valeu", "vlw", "ok", "okay", "blz",
             "beleza", "certo", "tudo"}

_WEEKDAYS = {
    "segunda": 0, "terca": 1, "quarta": 2, "quinta": 3,
    "sexta": 4, "sabado": 5, "domingo": 6,
}


def classificar(texto: str, *, hoje: date, max_dias: int = 30) -> IntentResult:
    t = _norm(texto)
    palavras = t.split()
    n = len(palavras)

    # 1) SAIR — frase-âncora OU palavra-gatilho isolada em mensagem curta
    if any(p in t for p in _SAIR_PHRASES) or (n <= 4 and any(w in _SAIR_WORDS for w in palavras)):
        return IntentResult(INTENT_SAIR, 0.95, matched="sair")

    # 2) JA_PAGUEI — passado, sem negação
    if any(p in t for p in _PAGO_PHRASES) and not any(neg in t for neg in _PAGO_NEG):
        return IntentResult(INTENT_JA_PAGUEI, 0.9, matched="pago")

    # 3) RESCHEDULE — marcador de adiamento OU data extraível
    data = extrair_data_desejada(t, hoje=hoje, max_dias=max_dias)
    if data or any(k in t for k in _RESCHEDULE_MARKERS):
        return IntentResult(INTENT_RESCHEDULE, 0.85 if data else 0.65,
                            data_desejada=data, matched="reschedule")

    # 4) NOVO_LINK — menciona link/boleto + problema/pedido
    if any(b in t for b in _LINK_BASE) and any(k in t for k in _LINK_KEYS):
        return IntentResult(INTENT_NOVO_LINK, 0.8, matched="novo_link")

    # 5) SAUDACAO pura — curta, sem pergunta (silêncio = anti-loop)
    if n <= 3 and "?" not in texto and palavras and all(w in _SAUDACAO for w in palavras):
        return IntentResult(INTENT_SAUDACAO, 0.6, matched="saudacao")

    # 6) DUVIDA — fallback (handoff humano)
    return IntentResult(INTENT_DUVIDA, 0.3)


def extrair_data_desejada(texto: str, *, hoje: date, max_dias: int = 30) -> str | None:
    """Extrai a data desejada (ISO) de frases como 'amanhã', 'semana que vem',
    'dia 25', '25/07', 'daqui a 3 dias'. Clamp [hoje+1 .. hoje+max_dias]; fora -> None."""
    t = _norm(texto)
    cand: date | None = None

    m = re.search(r"daqui a\s+(\d{1,2})\s+dia", t)
    if m:
        cand = hoje + timedelta(days=int(m.group(1)))
    elif "depois de amanha" in t:
        cand = hoje + timedelta(days=2)
    elif "amanha" in t:
        cand = hoje + timedelta(days=1)
    elif "semana que vem" in t or "proxima semana" in t:
        cand = hoje + timedelta(days=7)
    elif "mes que vem" in t or "proximo mes" in t:
        cand = hoje + timedelta(days=30)
    else:
        m = re.search(r"\b(\d{1,2})/(\d{1,2})\b", t)
        if m:
            cand = _proxima_data(hoje, int(m.group(1)), int(m.group(2)))
        else:
            m = re.search(r"\bdia\s+(\d{1,2})\b", t)
            if m:
                cand = _proximo_dia_do_mes(hoje, int(m.group(1)))
            else:
                for nome, wd in _WEEKDAYS.items():
                    if nome in t:
                        cand = _proximo_weekday(hoje, wd)
                        break

    if cand is None:
        return None
    if cand < hoje + timedelta(days=1) or cand > hoje + timedelta(days=max_dias):
        return None
    return cand.isoformat()


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _proxima_data(hoje: date, day: int, month: int) -> date | None:
    cand = _safe_date(hoje.year, month, day)
    if cand and cand < hoje:
        cand = _safe_date(hoje.year + 1, month, day)
    return cand


def _proximo_dia_do_mes(hoje: date, day: int) -> date | None:
    cand = _safe_date(hoje.year, hoje.month, day)
    if cand is None or cand <= hoje:
        m, y = hoje.month + 1, hoje.year
        if m > 12:
            m, y = 1, y + 1
        cand = _safe_date(y, m, day)
    return cand


def _proximo_weekday(hoje: date, wd: int) -> date:
    delta = (wd - hoje.weekday()) % 7
    return hoje + timedelta(days=delta or 7)


__all__ = [
    "IntentResult", "classificar", "extrair_data_desejada",
    "INTENT_SAIR", "INTENT_JA_PAGUEI", "INTENT_RESCHEDULE",
    "INTENT_NOVO_LINK", "INTENT_SAUDACAO", "INTENT_DUVIDA",
]
