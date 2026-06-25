"""FastAPI simplificado — Régua de Cobrança para Wladimir.

Endpoints:
  GET  /                          → UI (index.html)
  GET  /api/sessao?seguradora=    → verifica sessão ativa
  POST /api/login?seguradora=     → abre Chrome para login humano
  GET  /api/inadimplentes?seguradora= → lista clientes em atraso com dias
  POST /api/cobrar?seguradora=    → inicia cobrança (fire-and-forget)
  GET  /api/stream?seguradora=    → SSE com progresso da cobrança
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

_HERE = Path(__file__).parent


def create_app(workers: dict) -> FastAPI:
    """``workers`` é um dict {insurer: BrowserWorker}."""
    app = FastAPI(title="Régua de Cobrança")

    # Uma fila de log por seguradora (substituída a cada run de cobrança)
    _log_queues: dict[str, asyncio.Queue] = {}

    def _worker(seguradora: str):
        w = workers.get(seguradora)
        if w is None:
            raise HTTPException(status_code=400, detail=f"Seguradora não suportada: {seguradora}")
        return w

    @app.get("/")
    def index():
        return FileResponse(_HERE / "index.html")

    @app.get("/api/sessao")
    def sessao(seguradora: str = "prudential"):
        try:
            ativa = _worker(seguradora).check_session()
        except Exception:
            ativa = False
        return {"ativa": ativa, "seguradora": seguradora}

    @app.post("/api/login")
    def login(seguradora: str = "prudential"):
        try:
            _worker(seguradora).login()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"ok": True}

    @app.get("/api/inadimplentes")
    def inadimplentes(seguradora: str = "prudential"):
        try:
            return _worker(seguradora).discover()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/api/cobrar")
    def cobrar(seguradora: str = "prudential"):
        q: asyncio.Queue = asyncio.Queue()
        _log_queues[seguradora] = q
        _worker(seguradora).cobrar(q)
        return {"ok": True}

    @app.get("/api/stream")
    async def stream(seguradora: str = "prudential"):
        async def generator():
            # Aguarda a fila ser criada pelo POST /api/cobrar
            for _ in range(60):
                if seguradora in _log_queues:
                    break
                await asyncio.sleep(0.2)

            q = _log_queues.get(seguradora)
            if q is None:
                yield f"data: {json.dumps({'msg': 'Nenhuma cobrança em andamento.', 'fim': True})}\n\n"
                return

            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=30)
                except asyncio.TimeoutError:
                    yield "data: {}\n\n"  # keepalive
                    continue
                yield f"data: {json.dumps(msg)}\n\n"
                if msg.get("fim"):
                    _log_queues.pop(seguradora, None)
                    break

        return StreamingResponse(generator(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app
