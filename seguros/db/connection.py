"""Abertura e bootstrap idempotente do banco SQLite."""

from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA = Path(__file__).with_name("schema.sql")


def get_conn(db_path: Path | str) -> sqlite3.Connection:
    """Abre a conexão com ``row_factory`` = :class:`sqlite3.Row` e FKs ligadas."""
    path = Path(db_path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path | str) -> sqlite3.Connection:
    """Cria o schema (idempotente) e devolve a conexão pronta."""
    conn = get_conn(db_path)
    conn.executescript(_SCHEMA.read_text(encoding="utf-8"))
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Migrações leves: adiciona colunas novas a bancos já existentes."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(clientes_regua)")}
    novas = {
        "work_status": "TEXT",
        "follow_up_enviado_em": "TEXT",
        "primeiro_disparo_em": "TEXT",
        "resolvido_em": "TEXT",
        "tempo_ate_pagar_horas": "REAL",
        "conversao_atribuida": "INTEGER NOT NULL DEFAULT 0",
        "valor_recuperado_cents": "INTEGER",
        "ultimo_check_em": "TEXT",
        "checks_count": "INTEGER NOT NULL DEFAULT 0",
    }
    for nome, tipo in novas.items():
        if nome not in cols:
            conn.execute(f"ALTER TABLE clientes_regua ADD COLUMN {nome} {tipo}")


__all__ = ["get_conn", "init_db"]
