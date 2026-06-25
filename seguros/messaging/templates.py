"""Templates das mensagens + renderização segura + formatação BR.

Usamos ``string.Template`` (``${var}``) com ``safe_substitute``: é à prova de
chaves literais em URLs/nomes (que quebrariam ``str.format``) e, se faltar uma
variável, deixa o placeholder visível em vez de estourar — o dry-run pega isso.
"""

from __future__ import annotations

import html
from decimal import Decimal
from string import Template

# --- textos (exatamente os da especificação §10, em estilo ${var}) -----------

WHATSAPP_DIA0 = Template(
    """Olá, ${primeiro_nome}! Aqui é a IA do ${nome_corretor}, seu corretor de seguros.
Percebi que você tem um prêmio em aberto referente a ${competencia}, no valor de ${valor_total} — pode ser algum erro na cobrança ou algo que passou despercebido.
Para resolver de forma rápida e segura, é só acessar o link abaixo:
${link_pagamento}
Se já tiver pago, pode desconsiderar. Qualquer dúvida, estou aqui para ajudar.
Caso não queira mais receber estes avisos, responda SAIR."""
)

# Follow-up (dia 2): 2º toque, sutil/leve, abre a porta pra conversa.
WHATSAPP_FOLLOWUP = Template(
    """Olá, ${primeiro_nome}! Aqui é a IA do ${nome_corretor} de novo.
Passando só para lembrar que o prêmio referente a ${competencia} (${valor_total}) ainda consta em aberto. O link que te enviei vence hoje:
${link_pagamento}
Se já tiver pago, pode desconsiderar! Se precisar de um novo link, é só me avisar.
Abraço, ${nome_corretor}"""
)

# --- Variantes LEMBRETE (seguradora sem link de pagamento, ex.: Prudential) ---
# Sem ${link_pagamento} e sem citar a seguradora: a régua só lembra e abre a
# porta para a corretora orientar o pagamento pelo canal próprio.

WHATSAPP_DIA0_LEMBRETE = Template(
    """Olá, ${primeiro_nome}! Aqui é a IA do ${nome_corretor}, seu corretor de seguros.
Percebi que você tem um prêmio em aberto referente a ${competencia}, no valor de ${valor_total} — pode ser algum erro na cobrança ou algo que passou despercebido.
Me responda por aqui que eu te oriento sobre o pagamento.
Se já tiver pago, pode desconsiderar. Qualquer dúvida, estou aqui para ajudar.
Caso não queira mais receber estes avisos, responda SAIR."""
)

WHATSAPP_FOLLOWUP_LEMBRETE = Template(
    """Olá, ${primeiro_nome}! Aqui é a IA do ${nome_corretor} de novo.
Passando só para lembrar que o prêmio referente a ${competencia} (${valor_total}) ainda consta em aberto.
Se já tiver pago, pode desconsiderar! Se quiser, me chama que eu te ajudo a regularizar.
Abraço, ${nome_corretor}"""
)

EMAIL_DIA2_ASSUNTO_LEMBRETE = Template("Pendência no seu seguro — ${competencia}")

EMAIL_DIA2_TEXTO_LEMBRETE = Template(
    """Olá, ${primeiro_nome},

Identifiquei uma pendência no seu seguro referente a ${competencia}, no valor de ${valor_total}, ainda em aberto.

Para regularizar com tranquilidade, é só responder este e-mail ou me chamar que eu te oriento sobre o pagamento.

Se o pagamento já tiver sido feito, por favor desconsidere este e-mail.
Fico à disposição para qualquer dúvida.

Atenciosamente,
${nome_corretor} — ${corretora}"""
)

EMAIL_DIA2_HTML_LEMBRETE = Template(
    """<!DOCTYPE html>
<html lang="pt-BR">
<body style="font-family: Arial, Helvetica, sans-serif; font-size: 15px; color: #222; line-height: 1.5;">
  <p>Olá, ${primeiro_nome},</p>
  <p>Identifiquei uma pendência no seu seguro referente a <strong>${competencia}</strong>,
     no valor de <strong>${valor_total}</strong>, ainda em aberto.</p>
  <p>Para regularizar com tranquilidade, é só responder este e-mail ou me chamar
     que eu te oriento sobre o pagamento.</p>
  <p>Se o pagamento já tiver sido feito, por favor desconsidere este e-mail.<br>
     Fico à disposição para qualquer dúvida.</p>
  <p>Atenciosamente,<br>${nome_corretor} — ${corretora}</p>
</body>
</html>"""
)

# --- Agente inbound: respostas automáticas ao cliente ------------------------

RESP_SAIR = Template(
    "Tudo certo, ${primeiro_nome}! Não vou mais te enviar avisos por aqui. "
    "Se precisar de algo sobre seu seguro, é só chamar o ${nome_corretor}. Abraço!"
)
RESP_JA_PAGUEI_CONFIRMADO = Template(
    "Perfeito, ${primeiro_nome}! Confirmei aqui que está tudo regularizado. "
    "Obrigado e qualquer coisa estou à disposição."
)
RESP_JA_PAGUEI_PENDENTE = Template(
    "Obrigado por avisar, ${primeiro_nome}! Pelo sistema ainda consta um valor "
    "em aberto (${valor_total}). Às vezes a baixa leva algumas horas — se você já "
    "pagou, pode ignorar. O link segue válido, caso precise:\n${link_pagamento}"
)
RESP_JA_PAGUEI_VERIFICANDO = Template(
    "Obrigado, ${primeiro_nome}! Vou confirmar a baixa e já te retorno por aqui."
)
RESP_RESCHEDULE_OK = Template(
    "Sem problema, ${primeiro_nome}! Anotei aqui para ${data_desejada} e já avisei o "
    "${nome_corretor}. Te retorno com o link atualizado. Qualquer mudança, é só avisar."
)
RESP_RESCHEDULE_SEM_DATA = Template(
    "Claro, ${primeiro_nome}! Para qual data fica melhor para você? Me diz o dia que "
    "eu deixo tudo anotado por aqui."
)
RESP_NOVO_LINK = Template(
    "Claro, ${primeiro_nome}! Aqui está o link de pagamento:\n${link_pagamento}\n"
    "Se tiver qualquer dificuldade para abrir, me avisa que eu te ajudo."
)
RESP_NOVO_LINK_SEM_LINK = Template(
    "Claro, ${primeiro_nome}! Vou verificar o link e já te reenvio por aqui. "
    "Qualquer coisa, estou à disposição."
)
RESP_DUVIDA = Template(
    "Oi, ${primeiro_nome}! Obrigado pela mensagem. Vou verificar com calma e já te "
    "retorno por aqui. Se for urgente, me avisa."
)

# --- Avisos ao admin (handoff humano) ----------------------------------------

NOTIFY_ADMIN_RESCHEDULE = Template(
    "🗓️ *Remarcação pedida*\nCliente: ${nome} (${cpf_fmt})\nData desejada: ${data_desejada}\n"
    'Mensagem: "${texto}"\nAção: confirmar a reemissão do boleto no painel.'
)
NOTIFY_ADMIN_DUVIDA = Template(
    '❓ *Dúvida de cliente*\nCliente: ${nome} (${cpf_fmt})\nMensagem: "${texto}"\n'
    "Ação: responder o cliente manualmente."
)
NOTIFY_ADMIN_JA_PAGUEI_SEM_LEITURA = Template(
    "⚠️ *Conferir pagamento*\nCliente: ${nome} (${cpf_fmt}) disse que pagou, mas não "
    'consegui ler o "Valor inadimplente" na MAG.\nMensagem: "${texto}"\nAção: conferir manualmente.'
)

EMAIL_DIA2_ASSUNTO = Template("Pendência no seu seguro MAG — ${competencia}")

EMAIL_DIA2_TEXTO = Template(
    """Olá, ${primeiro_nome},

Identifiquei uma pendência no seu seguro MAG referente a ${competencia}, no valor de ${valor_total}, ainda em aberto.

Você pode regularizar de forma rápida e segura por este link:
${link_pagamento}

Se o pagamento já tiver sido feito, por favor desconsidere este e-mail.
Fico à disposição para qualquer dúvida.

Atenciosamente,
${nome_corretor} — ${corretora}"""
)

# HTML leve: sem imagens/pixels (boa entregabilidade), link clicável.
EMAIL_DIA2_HTML = Template(
    """<!DOCTYPE html>
<html lang="pt-BR">
<body style="font-family: Arial, Helvetica, sans-serif; font-size: 15px; color: #222; line-height: 1.5;">
  <p>Olá, ${primeiro_nome},</p>
  <p>Identifiquei uma pendência no seu seguro MAG referente a <strong>${competencia}</strong>,
     no valor de <strong>${valor_total}</strong>, ainda em aberto.</p>
  <p>Você pode regularizar de forma rápida e segura por este link:<br>
     <a href="${link_pagamento}" style="color: #0b5fff;">${link_pagamento}</a></p>
  <p>Se o pagamento já tiver sido feito, por favor desconsidere este e-mail.<br>
     Fico à disposição para qualquer dúvida.</p>
  <p>Atenciosamente,<br>${nome_corretor} — ${corretora}</p>
</body>
</html>"""
)


# --- formatação --------------------------------------------------------------


def brl_from_cents(cents: int | None) -> str:
    """Formata centavos em ``R$ 1.234,56`` (sem depender de ``locale``)."""
    if cents is None:
        return "R$ —"
    return _brl(Decimal(cents) / 100)


def _brl(value: Decimal) -> str:
    s = f"{value:,.2f}"  # 1,234.56  (padrão en_US)
    s = s.replace(",", "§").replace(".", ",").replace("§", ".")  # -> 1.234,56
    return f"R$ {s}"


def primeiro_nome(nome: str | None) -> str:
    partes = (nome or "").strip().split()
    return partes[0].capitalize() if partes else ""


# --- renderização ------------------------------------------------------------


def render(template: Template, ctx: dict, *, escape_html: bool = False) -> str:
    if escape_html:
        ctx = {k: html.escape(str(v)) for k, v in ctx.items()}
    return template.safe_substitute(ctx)


__all__ = [
    "WHATSAPP_DIA0",
    "WHATSAPP_FOLLOWUP",
    "WHATSAPP_DIA0_LEMBRETE",
    "WHATSAPP_FOLLOWUP_LEMBRETE",
    "EMAIL_DIA2_ASSUNTO_LEMBRETE",
    "EMAIL_DIA2_TEXTO_LEMBRETE",
    "EMAIL_DIA2_HTML_LEMBRETE",
    "RESP_SAIR",
    "RESP_JA_PAGUEI_CONFIRMADO",
    "RESP_JA_PAGUEI_PENDENTE",
    "RESP_JA_PAGUEI_VERIFICANDO",
    "RESP_RESCHEDULE_OK",
    "RESP_RESCHEDULE_SEM_DATA",
    "RESP_NOVO_LINK",
    "RESP_NOVO_LINK_SEM_LINK",
    "RESP_DUVIDA",
    "NOTIFY_ADMIN_RESCHEDULE",
    "NOTIFY_ADMIN_DUVIDA",
    "NOTIFY_ADMIN_JA_PAGUEI_SEM_LEITURA",
    "EMAIL_DIA2_ASSUNTO",
    "EMAIL_DIA2_TEXTO",
    "EMAIL_DIA2_HTML",
    "brl_from_cents",
    "primeiro_nome",
    "render",
]
