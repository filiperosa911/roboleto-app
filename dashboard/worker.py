"""Thread dedicada ao navegador por seguradora.

Playwright só pode ser usado na thread que o criou. Este worker recebe
comandos via queue, executa no contexto do browser e empurra logs para
o asyncio event loop principal (para o SSE).
"""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
from datetime import date
from typing import Any

log = logging.getLogger("roboleto.worker")


class _Notifier:
    """Adapta o push de log do worker para a interface .notify() esperada pelos conectores."""
    def __init__(self, push_fn):
        self._push = push_fn

    def notify(self, message: str) -> None:
        self._push(message)


class BrowserWorker:
    def __init__(self, config, loop: asyncio.AbstractEventLoop):
        self._config = config
        self._loop = loop
        self._cmd_q: queue.Queue = queue.Queue()
        self._log_q: asyncio.Queue | None = None  # atribuído antes do run
        self._connector = None
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"worker-{config.insurer}")
        self._thread.start()

    # --- API pública (chamada da thread do FastAPI) ---------------------------

    def check_session(self) -> bool:
        return self._call("check_session")

    def login(self) -> None:
        self._call("login", timeout=180)

    def discover(self) -> list[dict]:
        return self._call("discover", timeout=300)

    def cobrar(self, log_q: asyncio.Queue) -> None:
        """Inicia a cobrança completa. Logs vão para log_q (asyncio)."""
        self._log_q = log_q
        self._cmd_q.put(("cobrar", None, None))  # fire-and-forget

    def _call(self, cmd: str, timeout: int = 60) -> Any:
        result_q: queue.Queue = queue.Queue()
        self._cmd_q.put((cmd, None, result_q))
        status, value = result_q.get(timeout=timeout)
        if status == "err":
            raise value
        return value

    # --- loop interno (thread do browser) ------------------------------------

    def _run(self) -> None:
        while True:
            cmd, _args, result_q = self._cmd_q.get()
            try:
                result = self._dispatch(cmd)
                if result_q:
                    result_q.put(("ok", result))
            except Exception as exc:  # noqa: BLE001
                log.exception("worker %s erro em %s", self._config.insurer, cmd)
                if result_q:
                    result_q.put(("err", exc))

    def _dispatch(self, cmd: str) -> Any:
        if cmd == "check_session":
            return self._do_check_session()
        if cmd == "login":
            return self._do_login()
        if cmd == "discover":
            return self._do_discover()
        if cmd == "cobrar":
            self._do_cobrar()
            return None
        raise ValueError(f"comando desconhecido: {cmd}")

    def _get_connector(self):
        if self._connector is None:
            from seguros.connectors.factory import build_connector
            notifier = _Notifier(self._push_log)
            self._connector = build_connector(self._config, notifier=notifier)
            self._connector.start()
        return self._connector

    def _push_log(self, msg: str, nivel: str = "info") -> None:
        """Envia uma linha de log para o SSE (thread-safe)."""
        if self._log_q is None:
            return
        asyncio.run_coroutine_threadsafe(
            self._log_q.put({"msg": msg, "nivel": nivel}), self._loop
        )

    def _do_check_session(self) -> bool:
        try:
            connector = self._get_connector()
            connector.ensure_authenticated(interactive=False)
            return True
        except Exception:
            return False

    def _do_login(self) -> None:
        connector = self._get_connector()
        connector.ensure_authenticated(interactive=True)

    def _do_discover(self) -> list[dict]:
        connector = self._get_connector()
        connector.ensure_authenticated(interactive=True)
        delinquents = connector.discover_delinquents()
        today = date.today()
        result = []
        for d in delinquents:
            dias: int | None = None
            if d.vencimento_mais_antigo:
                try:
                    venc = date.fromisoformat(d.vencimento_mais_antigo[:10])
                    dias = max(0, (today - venc).days)
                except ValueError:
                    pass
            result.append({
                "cpf": d.cpf,
                "nome": d.nome or "—",
                "apolice": d.cpf,
                "valor": d.valor_texto or "—",
                "vencimento": (d.vencimento_mais_antigo or "")[:10],
                "dias_atraso": dias,
            })
        return result

    def _do_cobrar(self) -> None:
        import logging as _logging

        log_q = self._log_q

        def push(msg: str, nivel: str = "info") -> None:
            if log_q is None:
                return
            asyncio.run_coroutine_threadsafe(log_q.put({"msg": msg, "nivel": nivel}), self._loop)

        try:
            from seguros.config import config_for_insurer
            from seguros.connectors.factory import build_connector
            from seguros.db.connection import get_engine
            from seguros.db.repository import ClienteReguaRepository, LogRepository, OptOutRepository
            from seguros.messaging.email import SmtpEmailSender
            from seguros.messaging.whatsapp import ZApiSender
            from seguros.orchestrator import Orchestrator
            from seguros.report import RunReport

            cfg = self._config
            push(f"Iniciando cobrança — {cfg.insurer.upper()}", "info")

            engine = get_engine(str(cfg.db_path))
            repo = ClienteReguaRepository(engine)
            optout = OptOutRepository(engine)
            log_repo = LogRepository(engine)
            wa = ZApiSender(cfg.zapi_instance_id, cfg.zapi_token, cfg.zapi_client_token)
            email = SmtpEmailSender(cfg.gmail_address, cfg.gmail_app_password)

            connector = self._get_connector()

            class LogHandler(_logging.Handler):
                def emit(self_, record):
                    nivel = "warn" if record.levelno >= _logging.WARNING else "info"
                    push(record.getMessage(), nivel)

            handler = LogHandler()
            orch_log = _logging.getLogger("seguros")
            orch_log.addHandler(handler)

            try:
                report = Orchestrator(
                    config=cfg,
                    connector=connector,
                    repo=repo,
                    optout_repo=optout,
                    log_repo=log_repo,
                    wa_sender=wa,
                    email_sender=email,
                    report=RunReport(),
                ).run()
                total = len(report.rows) if report.rows else 0
                push(f"Cobrança concluída — {total} cliente(s) processado(s)", "ok")
            finally:
                orch_log.removeHandler(handler)

        except Exception as exc:  # noqa: BLE001
            push(f"Erro na cobrança: {exc}", "erro")
        finally:
            if log_q:
                asyncio.run_coroutine_threadsafe(log_q.put({"fim": True}), self._loop)
            self._log_q = None
