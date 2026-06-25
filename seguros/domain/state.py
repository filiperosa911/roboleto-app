"""Gates de decisão da régua — funções PURAS, sem I/O, totalmente testáveis.

Ordem dos gates (o primeiro que falha vence; na dúvida, NÃO envia):

1. opt_out          — quem pediu SAIR / foi marcado nunca é contatado
2. link presente    — sem link de pagamento não há o que cobrar
3. destino válido   — telefone normalizável (WhatsApp) / e-mail plausível (e-mail)
4. idempotência     — canal já disparado nesta régua
5. janela de horário (por último, para o relatório mostrar "enviaria, mas fora")

Consentimento NÃO é gate: o app gere relacionamento com clientes que já se
comunicam via WhatsApp. O controle de quem não contatar é o opt-out.
"""

from __future__ import annotations

from .models import Canal, ClienteRegua, Decision, Resultado


def evaluate(
    canal: Canal,
    *,
    opted_out: bool,
    tem_link: bool,
    destino_valido: bool,
    ja_enviado: bool,
    window_open: bool,
    requer_link: bool = True,
) -> Decision:
    """Avalia os gates a partir de booleanos já computados pelo orquestrador.

    ``requer_link``: seguradoras sem link de pagamento (ex.: Prudential — só
    lembrete) passam ``False`` para não pular por ``SEM_LINK``.
    """
    if opted_out:
        return Decision.skip(Resultado.PULADO_OPTOUT)
    if requer_link and not tem_link:
        return Decision.skip(Resultado.SEM_LINK)
    if not destino_valido:
        invalido = (
            Resultado.TELEFONE_INVALIDO if canal is Canal.WHATSAPP else Resultado.EMAIL_INVALIDO
        )
        return Decision.skip(invalido)
    if ja_enviado:
        return Decision.skip(Resultado.PULADO_IDEMPOTENTE)
    if not window_open:
        return Decision.defer(Resultado.PULADO_JANELA)
    return Decision.send()


def evaluate_whatsapp(
    cliente: ClienteRegua,
    *,
    opted_out: bool,
    telefone_valido: bool,
    tem_link: bool,
    window_open: bool,
    requer_link: bool = True,
) -> Decision:
    return evaluate(
        Canal.WHATSAPP,
        opted_out=opted_out,
        tem_link=tem_link,
        destino_valido=telefone_valido,
        ja_enviado=cliente.whatsapp_enviado_em is not None,
        window_open=window_open,
        requer_link=requer_link,
    )


def evaluate_email(
    cliente: ClienteRegua,
    *,
    opted_out: bool,
    email_valido: bool,
    tem_link: bool,
    window_open: bool,
    requer_link: bool = True,
) -> Decision:
    return evaluate(
        Canal.EMAIL,
        opted_out=opted_out,
        tem_link=tem_link,
        destino_valido=email_valido,
        ja_enviado=cliente.email_enviado_em is not None,
        window_open=window_open,
        requer_link=requer_link,
    )


__all__ = ["evaluate", "evaluate_whatsapp", "evaluate_email"]
