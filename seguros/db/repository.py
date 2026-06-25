"""DAO — único módulo que importa ``sqlite3`` fora de ``connection``.

Cada método mutante faz seu próprio ``commit`` para que um crash no meio do loop
deixe o banco consistente e o próximo run retome de onde parou (idempotência).
Todas as consultas são escopadas por ``corretor_id``.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from ..clock import iso_utc, now_utc, parse_iso
from ..domain.models import Canal, ClienteRegua, Modo, ReguaStatus, Resultado
from ..messaging.phone import canonical_brazilian_phone


def _row_to_cliente(row: sqlite3.Row) -> ClienteRegua:
    return ClienteRegua(
        cpf=row["cpf"],
        corretor_id=row["corretor_id"],
        nome=row["nome"],
        telefone=row["telefone"],
        email=row["email"],
        valor_inadimplente_cents=row["valor_inadimplente_cents"],
        valor_texto=row["valor_texto"],
        vencimento_mais_antigo=row["vencimento_mais_antigo"],
        competencia=row["competencia"],
        work_status=row["work_status"],
        link_pagamento=row["link_pagamento"],
        link_gerado_em=row["link_gerado_em"],
        autoriza_whatsapp=bool(row["autoriza_whatsapp"]),
        autoriza_email=bool(row["autoriza_email"]),
        whatsapp_enviado_em=row["whatsapp_enviado_em"],
        email_enviado_em=row["email_enviado_em"],
        follow_up_enviado_em=row["follow_up_enviado_em"],
        primeiro_disparo_em=row["primeiro_disparo_em"],
        resolvido_em=row["resolvido_em"],
        tempo_ate_pagar_horas=row["tempo_ate_pagar_horas"],
        conversao_atribuida=bool(row["conversao_atribuida"]),
        valor_recuperado_cents=row["valor_recuperado_cents"],
        ultimo_check_em=row["ultimo_check_em"],
        checks_count=row["checks_count"] or 0,
        enrolled_em=row["enrolled_em"],
        status=ReguaStatus(row["status"]),
        atualizado_em=row["atualizado_em"],
    )


class ReguaRepository:
    def __init__(self, conn: sqlite3.Connection, corretor_id: str = "local") -> None:
        self.conn = conn
        self.corretor_id = corretor_id

    def exists(self, cpf: str) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM clientes_regua WHERE corretor_id = ? AND cpf = ?",
            (self.corretor_id, cpf),
        )
        return cur.fetchone() is not None

    def get(self, cpf: str) -> ClienteRegua | None:
        cur = self.conn.execute(
            "SELECT * FROM clientes_regua WHERE corretor_id = ? AND cpf = ?",
            (self.corretor_id, cpf),
        )
        row = cur.fetchone()
        return _row_to_cliente(row) if row else None

    def all_clientes(self) -> list[ClienteRegua]:
        cur = self.conn.execute(
            "SELECT * FROM clientes_regua WHERE corretor_id = ? ORDER BY enrolled_em",
            (self.corretor_id,),
        )
        return [_row_to_cliente(r) for r in cur.fetchall()]

    def insert_enrollment(self, c: ClienteRegua) -> None:
        now = iso_utc()
        self.conn.execute(
            """
            INSERT INTO clientes_regua (
                cpf, corretor_id, nome, telefone, email,
                valor_inadimplente_cents, valor_texto, vencimento_mais_antigo,
                competencia, work_status, link_pagamento, link_gerado_em,
                autoriza_whatsapp, autoriza_email,
                whatsapp_enviado_em, email_enviado_em,
                enrolled_em, status, atualizado_em
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                c.cpf,
                self.corretor_id,
                c.nome,
                c.telefone,
                c.email,
                c.valor_inadimplente_cents,
                c.valor_texto,
                c.vencimento_mais_antigo,
                c.competencia,
                c.work_status,
                c.link_pagamento,
                c.link_gerado_em,
                int(c.autoriza_whatsapp),
                int(c.autoriza_email),
                c.whatsapp_enviado_em,
                c.email_enviado_em,
                c.enrolled_em or now,
                c.status.value,
                now,
            ),
        )
        self.conn.commit()

    def mark_whatsapp_sent(self, cpf: str, when_iso: str | None = None) -> None:
        ts = when_iso or iso_utc()
        self.conn.execute(
            "UPDATE clientes_regua SET whatsapp_enviado_em = ?, "
            "primeiro_disparo_em = COALESCE(primeiro_disparo_em, ?), atualizado_em = ? "
            "WHERE corretor_id = ? AND cpf = ?",
            (ts, ts, iso_utc(), self.corretor_id, cpf),
        )
        self.conn.commit()

    def mark_follow_up_sent(self, cpf: str, when_iso: str | None = None) -> None:
        self.conn.execute(
            "UPDATE clientes_regua SET follow_up_enviado_em = ?, atualizado_em = ? "
            "WHERE corretor_id = ? AND cpf = ?",
            (when_iso or iso_utc(), iso_utc(), self.corretor_id, cpf),
        )
        self.conn.commit()

    def mark_email_sent(self, cpf: str, when_iso: str | None = None) -> None:
        ts = when_iso or iso_utc()
        self.conn.execute(
            "UPDATE clientes_regua SET email_enviado_em = ?, "
            "primeiro_disparo_em = COALESCE(primeiro_disparo_em, ?), atualizado_em = ? "
            "WHERE corretor_id = ? AND cpf = ?",
            (ts, ts, iso_utc(), self.corretor_id, cpf),
        )
        self.conn.commit()

    def mark_resolved(self, cpf: str) -> dict:
        """Marca pagamento detectado (idempotente: 1ª detecção vence). Calcula o
        tempo até pagar e se a conversão é atribuída ao nosso disparo."""
        row = self.conn.execute(
            "SELECT resolvido_em, primeiro_disparo_em, valor_inadimplente_cents "
            "FROM clientes_regua WHERE corretor_id = ? AND cpf = ?",
            (self.corretor_id, cpf),
        ).fetchone()
        if row is None:
            return {"first_time": False}
        if row["resolvido_em"]:  # já resolvido — não sobrescreve
            return {"first_time": False, "atribuida": False}
        agora = iso_utc()
        pd = row["primeiro_disparo_em"]
        tempo_h = None
        atribuida = 0
        if pd:
            delta = (parse_iso(agora) - parse_iso(pd)).total_seconds()
            if delta >= 0:
                tempo_h = round(delta / 3600, 2)
                atribuida = 1
        self.conn.execute(
            "UPDATE clientes_regua SET status = 'resolvido', resolvido_em = ?, "
            "tempo_ate_pagar_horas = ?, conversao_atribuida = ?, "
            "valor_recuperado_cents = COALESCE(valor_recuperado_cents, valor_inadimplente_cents), "
            "atualizado_em = ? WHERE corretor_id = ? AND cpf = ?",
            (agora, tempo_h, atribuida, iso_utc(), self.corretor_id, cpf),
        )
        self.conn.commit()
        return {"first_time": True, "atribuida": bool(atribuida), "tempo_horas": tempo_h}

    def due_for_recheck(self, *, min_hours: int = 12, limit: int = 30) -> list[ClienteRegua]:
        """Fila de reconciliação: disparados, ainda em régua, sem check recente."""
        cutoff = iso_utc(now_utc() - timedelta(hours=min_hours))
        cur = self.conn.execute(
            "SELECT * FROM clientes_regua WHERE corretor_id = ? AND status = 'em_regua' "
            "AND (whatsapp_enviado_em IS NOT NULL OR email_enviado_em IS NOT NULL) "
            "AND (ultimo_check_em IS NULL OR ultimo_check_em <= ?) "
            "ORDER BY (ultimo_check_em IS NOT NULL), ultimo_check_em LIMIT ?",
            (self.corretor_id, cutoff, limit),
        )
        return [_row_to_cliente(r) for r in cur.fetchall()]

    def touch_check(self, cpf: str) -> None:
        self.conn.execute(
            "UPDATE clientes_regua SET ultimo_check_em = ?, checks_count = checks_count + 1 "
            "WHERE corretor_id = ? AND cpf = ?",
            (iso_utc(), self.corretor_id, cpf),
        )
        self.conn.commit()

    def set_status(self, cpf: str, status: ReguaStatus) -> None:
        self.conn.execute(
            "UPDATE clientes_regua SET status = ?, atualizado_em = ? "
            "WHERE corretor_id = ? AND cpf = ?",
            (status.value, iso_utc(), self.corretor_id, cpf),
        )
        self.conn.commit()

    def update_link(self, cpf: str, link: str, gerado_em_iso: str | None = None) -> None:
        self.conn.execute(
            "UPDATE clientes_regua SET link_pagamento = ?, link_gerado_em = ?, "
            "atualizado_em = ? WHERE corretor_id = ? AND cpf = ?",
            (link, gerado_em_iso or iso_utc(), iso_utc(), self.corretor_id, cpf),
        )
        self.conn.commit()

    def update_work_status(self, cpf: str, work_status: str | None) -> None:
        self.conn.execute(
            "UPDATE clientes_regua SET work_status = ?, atualizado_em = ? "
            "WHERE corretor_id = ? AND cpf = ?",
            (work_status, iso_utc(), self.corretor_id, cpf),
        )
        self.conn.commit()

    def update_telefone_if_missing(self, cpf: str, telefone: str | None) -> None:
        """Preenche o telefone apenas se ainda não estava salvo (não sobrescreve)."""
        if not telefone:
            return
        self.conn.execute(
            "UPDATE clientes_regua SET telefone = ?, atualizado_em = ? "
            "WHERE corretor_id = ? AND cpf = ? AND (telefone IS NULL OR telefone = '')",
            (telefone, iso_utc(), self.corretor_id, cpf),
        )
        self.conn.commit()

    def update_contact(self, cpf: str, *, telefone, email, autoriza_whatsapp,
                       autoriza_email) -> None:
        self.conn.execute(
            "UPDATE clientes_regua SET telefone = ?, email = ?, autoriza_whatsapp = ?, "
            "autoriza_email = ?, atualizado_em = ? WHERE corretor_id = ? AND cpf = ?",
            (telefone, email, int(bool(autoriza_whatsapp)), int(bool(autoriza_email)),
             iso_utc(), self.corretor_id, cpf),
        )
        self.conn.commit()

    def due_for_followup(self, offset_days: int, reference=None) -> list[ClienteRegua]:
        """Clientes em régua, sem e-mail enviado, com ``enrolled_em`` há >= offset dias."""
        ref = reference or now_utc()
        cutoff = (ref.date() - timedelta(days=offset_days)).isoformat()
        cur = self.conn.execute(
            """
            SELECT * FROM clientes_regua
            WHERE corretor_id = ?
              AND status = 'em_regua'
              AND email_enviado_em IS NULL
              AND substr(enrolled_em, 1, 10) <= ?
            ORDER BY enrolled_em
            """,
            (self.corretor_id, cutoff),
        )
        return [_row_to_cliente(r) for r in cur.fetchall()]

    def pending_whatsapp(self) -> list[ClienteRegua]:
        """Clientes em régua com WhatsApp autorizado, link gerado e ainda não enviado.

        Cobre o WhatsApp do dia 0 que foi adiado (fora da janela) ou falhou num run
        anterior — caso contrário ele se perderia (o cliente sai do filtro de
        descoberta após o "Cobrar").
        """
        cur = self.conn.execute(
            "SELECT * FROM clientes_regua WHERE corretor_id = ? AND status = 'em_regua' "
            "AND whatsapp_enviado_em IS NULL AND autoriza_whatsapp = 1 "
            "AND link_pagamento IS NOT NULL AND TRIM(link_pagamento) <> '' "
            "ORDER BY enrolled_em",
            (self.corretor_id,),
        )
        return [_row_to_cliente(r) for r in cur.fetchall()]

    def active_cpfs(self) -> set[str]:
        cur = self.conn.execute(
            "SELECT cpf FROM clientes_regua WHERE corretor_id = ? AND status = 'em_regua'",
            (self.corretor_id,),
        )
        return {r["cpf"] for r in cur.fetchall()}

    def find_cpf_by_telefone(self, telefone_canonical: str | None) -> str | None:
        """CPF do cliente cujo telefone casa (canonicalizando os dois lados — a MAG
        às vezes guarda sem o 9º dígito). Retorna None se 0 OU >1 match (ambiguidade
        nunca age no cliente errado)."""
        if not telefone_canonical:
            return None
        cur = self.conn.execute(
            "SELECT cpf, telefone FROM clientes_regua "
            "WHERE corretor_id = ? AND telefone IS NOT NULL AND TRIM(telefone) <> ''",
            (self.corretor_id,),
        )
        matches = {
            r["cpf"]
            for r in cur.fetchall()
            if canonical_brazilian_phone(r["telefone"]) == telefone_canonical
        }
        return next(iter(matches)) if len(matches) == 1 else None


class OptOutRepository:
    def __init__(self, conn: sqlite3.Connection, corretor_id: str = "local") -> None:
        self.conn = conn
        self.corretor_id = corretor_id

    def is_opted_out(self, *, cpf: str | None = None, telefone: str | None = None) -> bool:
        clauses, params = [], [self.corretor_id]
        if cpf:
            clauses.append("cpf = ?")
            params.append(cpf)
        if telefone:
            clauses.append("telefone = ?")
            params.append(telefone)
        if not clauses:
            return False
        cur = self.conn.execute(
            f"SELECT 1 FROM opt_out WHERE corretor_id = ? AND ({' OR '.join(clauses)}) LIMIT 1",
            params,
        )
        return cur.fetchone() is not None

    def add(self, *, cpf: str | None = None, telefone: str | None = None,
            origem: str = "manual") -> None:
        if cpf:  # idempotente por cpf (uq_optout_cpf); grava o telefone junto se houver
            self.conn.execute(
                "INSERT OR IGNORE INTO opt_out (corretor_id, cpf, telefone, origem, data) "
                "VALUES (?, ?, ?, ?, ?)",
                (self.corretor_id, cpf, telefone, origem, iso_utc()),
            )
        elif telefone and not self.is_opted_out(telefone=telefone):  # sem UNIQUE: dedupe manual
            self.conn.execute(
                "INSERT INTO opt_out (corretor_id, cpf, telefone, origem, data) "
                "VALUES (?, NULL, ?, ?, ?)",
                (self.corretor_id, telefone, origem, iso_utc()),
            )
        self.conn.commit()


class LogRepository:
    def __init__(self, conn: sqlite3.Connection, corretor_id: str = "local") -> None:
        self.conn = conn
        self.corretor_id = corretor_id

    def record(
        self,
        *,
        cpf: str,
        canal: Canal,
        resultado: Resultado | str,
        modo: Modo,
        link: str | None = None,
        payload_resumo: str | None = None,
    ) -> None:
        res = resultado.value if isinstance(resultado, Resultado) else str(resultado)
        self.conn.execute(
            "INSERT INTO log_disparos "
            "(corretor_id, cpf, canal, link, resultado, payload_resumo, modo, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.corretor_id,
                cpf,
                canal.value,
                link,
                res,
                payload_resumo,
                modo.value,
                iso_utc(),
            ),
        )
        self.conn.commit()


class StatusCheckRepository:
    def __init__(self, conn: sqlite3.Connection, corretor_id: str = "local") -> None:
        self.conn = conn
        self.corretor_id = corretor_id

    def record(self, *, cpf: str, all_regularized: bool, transicao: bool, origem: str) -> None:
        self.conn.execute(
            "INSERT INTO status_checks (corretor_id, cpf, all_regularized, transicao, origem, "
            "checked_em) VALUES (?, ?, ?, ?, ?, ?)",
            (self.corretor_id, cpf, int(all_regularized), int(transicao), origem, iso_utc()),
        )
        self.conn.commit()

    def pagamentos_por_dia(self, dias: int = 30) -> list[dict]:
        """Conversões (transições devendo->pago) por dia, p/ a curva de pagamentos."""
        cur = self.conn.execute(
            "SELECT substr(checked_em, 1, 10) dia, COUNT(*) n FROM status_checks "
            "WHERE corretor_id = ? AND transicao = 1 GROUP BY dia ORDER BY dia DESC LIMIT ?",
            (self.corretor_id, dias),
        )
        return [dict(r) for r in cur.fetchall()]


class InboundRepository:
    """Log de mensagens recebidas + GATE de idempotência atômico (must-fix #4)."""

    def __init__(self, conn: sqlite3.Connection, corretor_id: str = "local") -> None:
        self.conn = conn
        self.corretor_id = corretor_id

    def record(self, *, message_id, cpf, telefone, telefone_raw, sender_name, texto,
               intent, confianca, data_desejada, origem, outcome) -> tuple[int | None, bool]:
        """INSERT OR IGNORE atômico. Retorna (id, is_new). is_new=False quando o
        message_id já foi processado (a trava de idempotência). Com message_id=None
        (simulação) sempre insere (is_new=True)."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO inbound_messages (corretor_id, message_id, cpf, telefone, "
            "telefone_raw, sender_name, texto, intent, confianca, data_desejada, origem, "
            "outcome, recebido_em) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.corretor_id, message_id, cpf, telefone, telefone_raw, sender_name, texto,
             intent, confianca, data_desejada, origem, outcome, iso_utc()),
        )
        self.conn.commit()
        if cur.rowcount == 0:  # message_id duplicado -> já existe
            row = self.conn.execute(
                "SELECT id FROM inbound_messages WHERE corretor_id = ? AND message_id = ?",
                (self.corretor_id, message_id),
            ).fetchone()
            return (row["id"] if row else None, False)
        return (cur.lastrowid, True)

    def mark_outcome(self, inbound_id: int, outcome: str, *, cpf: str | None = None) -> None:
        if cpf is not None:
            self.conn.execute(
                "UPDATE inbound_messages SET outcome = ?, cpf = ? WHERE corretor_id = ? AND id = ?",
                (outcome, cpf, self.corretor_id, inbound_id),
            )
        else:
            self.conn.execute(
                "UPDATE inbound_messages SET outcome = ? WHERE corretor_id = ? AND id = ?",
                (outcome, self.corretor_id, inbound_id),
            )
        self.conn.commit()

    def recentes(self, limit: int = 50) -> list[dict]:
        cur = self.conn.execute(
            "SELECT * FROM inbound_messages WHERE corretor_id = ? ORDER BY id DESC LIMIT ?",
            (self.corretor_id, limit),
        )
        return [dict(r) for r in cur.fetchall()]


class RescheduleRepository:
    """Pedidos de remarcação ('quer outro dia') com máquina de estados."""

    def __init__(self, conn: sqlite3.Connection, corretor_id: str = "local") -> None:
        self.conn = conn
        self.corretor_id = corretor_id

    def create(self, *, cpf, data_desejada, texto_origem, inbound_id) -> int:
        now = iso_utc()
        cur = self.conn.execute(
            "INSERT INTO reschedule_requests (corretor_id, cpf, inbound_id, data_desejada, "
            "texto_origem, status, criado_em, atualizado_em) VALUES (?, ?, ?, ?, ?, 'aberto', ?, ?)",
            (self.corretor_id, cpf, inbound_id, data_desejada, texto_origem, now, now),
        )
        self.conn.commit()
        return cur.lastrowid

    def open_for_cpf(self, cpf: str) -> dict | None:
        """Pedido ainda em aberto p/ o CPF (dedupe de múltiplos pedidos — edge case)."""
        row = self.conn.execute(
            "SELECT * FROM reschedule_requests WHERE corretor_id = ? AND cpf = ? "
            "AND status IN ('aberto', 'admin_avisado') ORDER BY id DESC LIMIT 1",
            (self.corretor_id, cpf),
        ).fetchone()
        return dict(row) if row else None

    def mark_admin_notified(self, rid: int) -> None:
        now = iso_utc()
        self.conn.execute(
            "UPDATE reschedule_requests SET status = 'admin_avisado', admin_avisado_em = ?, "
            "atualizado_em = ? WHERE corretor_id = ? AND id = ? AND status = 'aberto'",
            (now, now, self.corretor_id, rid),
        )
        self.conn.commit()

    def mark_link_reenviado(self, rid: int, link: str) -> None:
        now = iso_utc()
        self.conn.execute(
            "UPDATE reschedule_requests SET status = 'link_reenviado', link_novo = ?, "
            "link_reenviado_em = ?, atualizado_em = ? WHERE corretor_id = ? AND id = ?",
            (link, now, now, self.corretor_id, rid),
        )
        self.conn.commit()

    def pendentes(self) -> list[dict]:
        cur = self.conn.execute(
            "SELECT * FROM reschedule_requests WHERE corretor_id = ? "
            "AND status IN ('aberto', 'admin_avisado') ORDER BY id DESC",
            (self.corretor_id,),
        )
        return [dict(r) for r in cur.fetchall()]

    def get(self, rid: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM reschedule_requests WHERE corretor_id = ? AND id = ?",
            (self.corretor_id, rid),
        ).fetchone()
        return dict(row) if row else None


__all__ = [
    "ReguaRepository",
    "OptOutRepository",
    "LogRepository",
    "StatusCheckRepository",
    "InboundRepository",
    "RescheduleRepository",
]
