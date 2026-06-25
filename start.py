"""Ponto de entrada do roboleto-app.

Sobe o servidor FastAPI e abre o Chrome no dashboard.
Execute com:  python start.py
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
import time
import webbrowser

import uvicorn

URL = "http://127.0.0.1:8765"


def _abrir_browser():
    time.sleep(2)
    webbrowser.open(URL)


def main() -> None:
    from seguros.config import load_config, config_for_insurer
    from seguros.logging_setup import configure_logging
    from dashboard.worker import BrowserWorker
    from dashboard.app import create_app

    configure_logging()

    cfg_base = load_config(live=True)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    cfg_prudential = config_for_insurer(cfg_base, "prudential")
    cfg_mag        = config_for_insurer(cfg_base, "mag")

    workers = {
        "prudential": BrowserWorker(cfg_prudential, loop),
        "mag":        BrowserWorker(cfg_mag,        loop),
    }

    app = create_app(workers)

    threading.Thread(target=_abrir_browser, daemon=True).start()

    print(f"\n  Régua de Cobrança rodando em {URL}\n  (Ctrl+C para encerrar)\n")

    uvicorn.run(app, host="127.0.0.1", port=8765, loop="none", log_level="warning")


if __name__ == "__main__":
    main()
