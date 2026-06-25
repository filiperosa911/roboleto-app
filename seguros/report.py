"""Relatório do run: linhas acumuladas -> CSV + resumo no console.

O CSV é a peça central do dry-run: mostra, por (cliente, canal), a decisão, o
motivo e a MENSAGEM RENDERIZADA que seria enviada.
"""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .clock import now_utc

_COLUMNS = [
    "cpf",
    "nome",
    "primeiro_nome",
    "canal",
    "dia_regua",
    "decisao",
    "destino",
    "competencia",
    "valor_total",
    "link_pagamento",
    "mensagem_renderizada",
    "detalhe",
    "timestamp",
]


@dataclass
class ReportRow:
    cpf: str
    nome: str
    primeiro_nome: str
    canal: str
    dia_regua: int
    decisao: str  # resultado (enviado / dry_run / pulado_* / erro)
    destino: str
    competencia: str
    valor_total: str
    link_pagamento: str
    mensagem_renderizada: str
    detalhe: str = ""

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["timestamp"] = now_utc().isoformat()
        return d


class RunReport:
    def __init__(self, *, live: bool) -> None:
        self.live = live
        self.rows: list[ReportRow] = []

    def add(self, row: ReportRow) -> None:
        self.rows.append(row)

    def write_csv(self, reports_dir: Path) -> Path:
        reports_dir.mkdir(parents=True, exist_ok=True)
        stamp = now_utc().strftime("%Y%m%d-%H%M%S")
        prefix = "live" if self.live else "dry-run"
        path = reports_dir / f"{prefix}-{stamp}.csv"
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=_COLUMNS)
            writer.writeheader()
            for r in self.rows:
                writer.writerow(r.as_dict())
        return path

    def console_summary(self, csv_path: Path | None = None) -> str:
        modo = "LIVE" if self.live else "DRY-RUN"
        lines = [f"\n=== Régua MAG — resumo [{modo}] ==="]
        for canal in ("whatsapp", "email"):
            counts = Counter(r.decisao for r in self.rows if r.canal == canal)
            if not counts:
                continue
            partes = " | ".join(f"{n} {dec}" for dec, n in sorted(counts.items()))
            lines.append(f"{canal:9s}: {partes}")
        lines.append(f"Total de avaliações: {len(self.rows)}")
        if csv_path:
            lines.append(f"Relatório CSV: {csv_path}")
        if not self.live:
            lines.append(">>> DRY-RUN: nada foi enviado nem alterado na MAG. Use --live para valer. <<<")
        return "\n".join(lines)


__all__ = ["RunReport", "ReportRow"]
