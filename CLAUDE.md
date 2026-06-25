# roboleto-app — Contexto para o Agente

## O que é este projeto

Dashboard simplificado de régua de cobrança para **Wladimir Leis** (Life Planner),
derivado do projeto `magprud`. Suporta **MAG Seguros** e **Prudential**.

Objetivo: o Wladimir dá duplo clique no `iniciar.bat`, o Chrome abre no dashboard,
ele clica "Carregar Inadimplentes" e depois "Iniciar Cobrança" — o robô faz tudo
(raspa dados, gera boletos PDF, envia WhatsApp).

## Repos relacionados

- **Este projeto:** https://github.com/filiperosa911/roboleto-app ← você está aqui
- **Projeto original (não mexer):** https://github.com/filiperosa911/magprud

## Como rodar

```powershell
# Na pasta raiz do projeto:
.venv\Scripts\python.exe start.py
# Abre o dashboard em http://127.0.0.1:8765
```

Se o `.venv` não existir ainda:
```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\playwright install chromium
```

## Estrutura

```
roboleto-app/
├── start.py               ← entry point (FastAPI + abre browser)
├── iniciar.bat            ← atalho para o Wladimir (instala + inicia)
├── .env                   ← configuração local (não commitado)
├── .env.example           ← modelo de configuração
├── dashboard/
│   ├── app.py             ← endpoints FastAPI
│   ├── worker.py          ← thread do Playwright por seguradora
│   └── index.html         ← UI completa (abas, tabela, log SSE)
└── seguros/               ← pacote copiado do magprud
    ├── connectors/
    │   ├── prudential/    ← conector Prudential (boleto via shadow DOM)
    │   └── mag/           ← conector MAG
    ├── db/                ← SQLite
    ├── messaging/         ← WhatsApp (Z-API), e-mail
    └── orchestrator.py    ← lógica de régua completa
```

## Status atual (2026-06-25)

- [x] Dashboard abre com abas MAG / Prudential
- [x] Tabela de inadimplentes com coluna "Dias em Atraso" e ordenação por coluna
- [x] Sessões copiadas de `.prudential_session/` e `.mag_session/` (cookies válidos)
- [ ] **Pendente: testar "Carregar Inadimplentes" → tabela aparece corretamente**
- [ ] **Pendente: testar "Iniciar Cobrança" → PDFs salvos localmente**
- [ ] WhatsApp desabilitado no .env de teste (ZAPI_INSTANCE_ID=teste)

## .env de teste atual

```
CORRETOR_ID=wladimir
NOME_CORRETOR=Wladimir Leis
NOME_CORRETORA=Wladimir Leis - Life Planner
ZAPI_INSTANCE_ID=teste        # dummy — WhatsApp é ignorado nos testes
ZAPI_TOKEN=teste
DB_PATH=C:/Users/filip/AppData/Local/roboleto/regua_wlad.sqlite
HORARIO_INICIO=08:00
HORARIO_FIM=20:00
DIAS_UTEIS_APENAS=false
TIMEZONE=America/Sao_Paulo
```

## Decisões técnicas importantes

**Por que Playwright roda em thread separada?**
Playwright só pode ser usado na thread que o criou. O `BrowserWorker` (worker.py)
mantém uma thread dedicada por seguradora e recebe comandos via `queue.Queue`.

**Por que o botão de download usa `iron-icon="cr:file-download"`?**
O Chrome PDF viewer tem dois botões com `id="save"`: um para download local e outro
para o Google Drive. O seletor `iron-icon="cr:file-download"` identifica só o local.

**Por que Z-API com valores fictícios funciona para teste?**
O orchestrator faz `wa.healthcheck()` no início — se falhar, seta `_wa_disabled=True`
e continua o run sem WhatsApp. Boletos são gerados normalmente.

**Sessões do Playwright**
Ficam em `.prudential_session/` e `.mag_session/` na raiz do projeto.
Copiadas do magprud. Quando expirarem, o botão "Fazer Login" no dashboard
abre o Chrome para o usuário autenticar manualmente (acontece a cada 2–4 semanas).

## Próximos passos planejados

1. Testar o fluxo completo no novo dashboard
2. Configurar Z-API real quando Wladimir tiver as credenciais
3. Migrar envio para número autenticado pela Meta (WhatsApp Business API oficial)
4. Preparar instalação no PC do Wladimir (copiar pasta + criar atalho na área de trabalho)
