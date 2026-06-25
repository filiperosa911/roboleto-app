-- Schema da régua de cobrança. Tudo IF NOT EXISTS -> init idempotente.
-- Convenções: datetime = TEXT ISO-8601 UTC ("...Z"); bool = INTEGER 0/1;
-- dinheiro = INTEGER centavos; CPF = TEXT 11 dígitos.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS clientes_regua (
    cpf                      TEXT NOT NULL,
    corretor_id              TEXT NOT NULL DEFAULT 'local',
    nome                     TEXT NOT NULL,
    telefone                 TEXT,
    email                    TEXT,
    valor_inadimplente_cents INTEGER,
    valor_texto              TEXT,
    vencimento_mais_antigo   TEXT,
    competencia              TEXT,
    work_status              TEXT,
    link_pagamento           TEXT,
    link_gerado_em           TEXT,
    autoriza_whatsapp        INTEGER NOT NULL DEFAULT 0 CHECK (autoriza_whatsapp IN (0, 1)),
    autoriza_email           INTEGER NOT NULL DEFAULT 0 CHECK (autoriza_email IN (0, 1)),
    whatsapp_enviado_em      TEXT,
    email_enviado_em         TEXT,
    follow_up_enviado_em     TEXT,
    primeiro_disparo_em      TEXT,
    resolvido_em             TEXT,
    tempo_ate_pagar_horas    REAL,
    conversao_atribuida      INTEGER NOT NULL DEFAULT 0 CHECK (conversao_atribuida IN (0, 1)),
    valor_recuperado_cents   INTEGER,
    ultimo_check_em          TEXT,
    checks_count             INTEGER NOT NULL DEFAULT 0,
    enrolled_em              TEXT NOT NULL,
    status                   TEXT NOT NULL DEFAULT 'em_regua'
                               CHECK (status IN ('em_regua', 'resolvido', 'opt_out')),
    atualizado_em            TEXT NOT NULL,
    PRIMARY KEY (corretor_id, cpf)
);
CREATE INDEX IF NOT EXISTS idx_regua_status   ON clientes_regua(corretor_id, status);
CREATE INDEX IF NOT EXISTS idx_regua_enrolled ON clientes_regua(enrolled_em);

CREATE TABLE IF NOT EXISTS opt_out (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    corretor_id TEXT NOT NULL DEFAULT 'local',
    cpf         TEXT,
    telefone    TEXT,
    origem      TEXT NOT NULL CHECK (origem IN ('sair_whatsapp', 'manual')),
    data        TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_optout_cpf
    ON opt_out(corretor_id, cpf) WHERE cpf IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_optout_tel ON opt_out(corretor_id, telefone);

CREATE TABLE IF NOT EXISTS log_disparos (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    corretor_id    TEXT NOT NULL DEFAULT 'local',
    cpf            TEXT NOT NULL,
    canal          TEXT NOT NULL CHECK (canal IN ('whatsapp', 'email', 'sistema')),
    link           TEXT,
    resultado      TEXT NOT NULL,
    payload_resumo TEXT,
    modo           TEXT NOT NULL CHECK (modo IN ('dry_run', 'live')),
    data           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_log_cpf  ON log_disparos(corretor_id, cpf);
CREATE INDEX IF NOT EXISTS idx_log_data ON log_disparos(data);

-- Histórico append-only de checagens de status (base da análise de conversão).
CREATE TABLE IF NOT EXISTS status_checks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    corretor_id     TEXT NOT NULL DEFAULT 'local',
    cpf             TEXT NOT NULL,
    all_regularized INTEGER NOT NULL CHECK (all_regularized IN (0, 1)),
    transicao       INTEGER NOT NULL DEFAULT 0,  -- 1 = este check virou para resolvido
    origem          TEXT NOT NULL,
    checked_em      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checks_cpf  ON status_checks(corretor_id, cpf, checked_em);
CREATE INDEX IF NOT EXISTS idx_checks_data ON status_checks(checked_em);

-- Agente inbound: log append-only de TODA mensagem recebida (real ou simulada).
-- Também é o GATE de idempotência (UNIQUE em message_id): Z-API reenvia em lentidão.
CREATE TABLE IF NOT EXISTS inbound_messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    corretor_id   TEXT NOT NULL DEFAULT 'local',
    message_id    TEXT,                 -- id do Z-API (NULL em simulação)
    cpf           TEXT,                 -- resolvido (NULL se não mapeou)
    telefone      TEXT,                 -- remetente canonical
    telefone_raw  TEXT,                 -- phone cru do payload (auditoria)
    sender_name   TEXT,
    texto         TEXT NOT NULL,
    intent        TEXT NOT NULL,
    confianca     REAL,
    data_desejada TEXT,                 -- ISO date extraída no reschedule (NULL)
    origem        TEXT NOT NULL CHECK (origem IN ('webhook', 'simulacao')),
    outcome       TEXT NOT NULL,
    recebido_em   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_inbound_msgid
    ON inbound_messages(corretor_id, message_id) WHERE message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_inbound_cpf ON inbound_messages(corretor_id, cpf);

-- Pedidos de remarcação ("quer outro dia"): registra + controla aviso ao admin
-- e a (eventual) reemissão do link. Estado de máquina por linha (histórico por CPF).
CREATE TABLE IF NOT EXISTS reschedule_requests (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    corretor_id         TEXT NOT NULL DEFAULT 'local',
    cpf                 TEXT NOT NULL,
    inbound_id          INTEGER,
    data_desejada       TEXT,           -- ISO date YYYY-MM-DD ou NULL
    texto_origem        TEXT,
    status              TEXT NOT NULL DEFAULT 'aberto'
                          CHECK (status IN ('aberto','admin_avisado','link_reenviado','concluido','cancelado')),
    admin_avisado_em    TEXT,
    link_novo           TEXT,
    link_reenviado_em   TEXT,
    criado_em           TEXT NOT NULL,
    atualizado_em       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resched_cpf    ON reschedule_requests(corretor_id, cpf);
CREATE INDEX IF NOT EXISTS idx_resched_status ON reschedule_requests(corretor_id, status);

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', '2');
UPDATE schema_meta SET value = '3' WHERE key = 'schema_version' AND value = '2';
