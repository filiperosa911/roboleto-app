"""CLI / entrypoint — wiring de tudo.

    python -m seguros                 # DRY-RUN (padrão): descobre, casa, renderiza, NÃO envia
    python -m seguros --live          # envios e geração de link REAIS
    python -m seguros --login         # só pré-autentica (resolve login/captcha 1x)
    python -m seguros --inspect       # calibra selectors.yaml (Playwright Inspector)
    python -m seguros --validate-selectors
    python -m seguros --fake          # roda offline com FakeConnector (sem MAG/Playwright)
    python -m seguros --add-optout 12345678909
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from .config import ConfigError, config_for_insurer, load_config
from .cpf import normalize_cpf
from .db.connection import init_db
from .db.repository import LogRepository, OptOutRepository, ReguaRepository
from .logging_setup import setup_logging
from .notify import NotificationService
from .orchestrator import CircuitBreaker, Orchestrator
from .report import RunReport

log = logging.getLogger("seguros.cli")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="seguros", description="Régua de cobrança MAG + Z-API")
    p.add_argument("--live", action="store_true",
                   help="habilita geração de link real (clica Cobrar) e envios reais")
    p.add_argument("--login", action="store_true", help="apenas pré-autenticar na MAG")
    p.add_argument("--inspect", action="store_true", help="calibrar selectors.yaml")
    p.add_argument("--validate-selectors", action="store_true",
                   help="conferir se os seletores resolvem na página")
    p.add_argument("--fake", action="store_true",
                   help="rodar com FakeConnector (offline, sem MAG/Playwright)")
    p.add_argument("--limit", type=int, default=None, help="processa no máximo N inadimplentes")
    p.add_argument("--add-optout", metavar="CPF", help="adiciona um CPF à lista de opt-out e sai")
    p.add_argument("--test-whatsapp", metavar="NUMERO", nargs="?", const="",
                   help="testa a conexão Z-API e envia 1 msg de teste (default: WHATSAPP_OVERRIDE_TO)")
    p.add_argument("--env", default=None, help="caminho do arquivo .env")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument("--dashboard", action="store_true", help="abre o painel web local")
    p.add_argument("--port", type=int, default=8765, help="porta do dashboard")
    p.add_argument("--insurer", choices=["mag", "prudential"], default=None,
                   help="seguradora alvo (default: INSURER do .env ou 'mag')")
    return p.parse_args(argv)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        os.kill(pid, 0)
        return True
    except (OSError, ValueError, AttributeError):
        return False


def _acquire_lock(db_path: Path) -> Path | None:
    lock_path = db_path.with_suffix(db_path.suffix + ".lock")

    def _create() -> Path | None:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return lock_path

    try:
        return _create()
    except FileExistsError:
        # lock existe: pode ser obsoleto (dono morto por crash/kill).
        try:
            owner = int(lock_path.read_text().strip() or "-1")
        except (OSError, ValueError):
            owner = -1
        if not _pid_alive(owner):
            log.warning("lock obsoleto (pid=%s morto) — readquirindo", owner)
            try:
                lock_path.unlink()
                return _create()
            except OSError:
                pass
        print(f"Outra execução parece estar rodando (lock: {lock_path}). "
              f"Se tiver certeza que não, apague o arquivo.")
        return None


def _release_lock(lock_path: Path | None) -> None:
    if lock_path and lock_path.exists():
        try:
            lock_path.unlink()
        except OSError:
            pass


def _build_notifier(config) -> NotificationService:
    client = None
    if config.zapi_instance_id and config.zapi_token:
        from .messaging.whatsapp import ZApiClient

        client = ZApiClient(config.zapi_instance_id, config.zapi_token, config.zapi_client_token)
    return NotificationService(zapi_client=client, notify_to=config.notify_whatsapp_to or None)


def _build_senders(config):
    from .messaging.email import DryRunEmail
    from .messaging.whatsapp import DryRunWhatsApp

    if not config.live:
        return DryRunWhatsApp(), DryRunEmail()

    from .messaging.whatsapp import ZApiClient, ZApiSender

    client = ZApiClient(config.zapi_instance_id, config.zapi_token, config.zapi_client_token)
    wa = ZApiSender(client, pacing_min_s=config.pacing_min_s, pacing_max_s=config.pacing_max_s)

    # Gmail opcional: sem credenciais, o canal de e-mail (dia 2) fica em dry-run.
    if config.gmail_address and config.gmail_app_password:
        from .messaging.email import SmtpSender

        email = SmtpSender(
            user=config.gmail_address,
            password=config.gmail_app_password,
            from_name=config.nome_corretor,
        )
    else:
        log.warning("Gmail não configurado — canal de e-mail (dia 2) DESLIGADO neste run.")
        email = DryRunEmail()
    return wa, email


def _force_utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_console()
    args = parse_args(argv)

    try:
        config = load_config(live=args.live, env_path=args.env)
    except ConfigError as err:
        print(err, file=sys.stderr)
        return 2

    # Seguradora alvo: --insurer sobrepõe o INSURER do .env e ajusta escopo de
    # dados (corretor_id:insurer) e pasta de sessão (.{insurer}_session).
    if args.insurer:
        config = config_for_insurer(config, args.insurer)

    setup_logging(Path("logs"), args.log_level)
    if config.live:
        log.warning("================ MODO LIVE ================")
        log.warning("Mensagens REAIS serão enviadas e a MAG será ALTERADA (Cobrar).")

    # dashboard web: sobe o servidor local e abre o navegador
    if args.dashboard:
        return _run_dashboard(config, args.port)

    conn = init_db(config.db_path)
    optout_repo = OptOutRepository(conn, config.corretor_id)

    # teste de conexão Z-API: não precisa de conector MAG
    if args.test_whatsapp is not None:
        rc = _test_whatsapp(config, args.test_whatsapp)
        conn.close()
        return rc

    # opt-out manual: não precisa de conector
    if args.add_optout:
        cpf = normalize_cpf(args.add_optout)
        optout_repo.add(cpf=cpf, origem="manual")
        print(f"opt-out adicionado para CPF {cpf}.")
        conn.close()
        return 0

    repo = ReguaRepository(conn, config.corretor_id)
    log_repo = LogRepository(conn, config.corretor_id)
    notifier = _build_notifier(config)

    calibration_mode = args.inspect or args.validate_selectors
    if args.fake and (calibration_mode or args.login):
        print("--inspect/--validate-selectors/--login exigem a seguradora real (não use --fake).",
              file=sys.stderr)
        conn.close()
        return 2

    lock = _acquire_lock(config.db_path)
    if lock is None:
        conn.close()
        return 4

    rc = 0
    try:
        # --login: feito num Chrome NORMAL (fora do Playwright) porque o reCAPTCHA
        # não passa no navegador automatizado; depois verifica a sessão.
        if args.login:
            rc = _do_login(config, notifier)
            return rc

        if args.fake:
            from .connectors.fake import FakeConnector

            connector = FakeConnector()
        else:
            from .connectors.factory import build_connector

            connector = build_connector(config, notifier=notifier)

        with connector:
            if args.inspect:
                run_inspect = _inspect_module(config).run_inspect

                run_inspect(connector, Path("artifacts"))
                return 0
            if args.validate_selectors:
                validate_selectors = _inspect_module(config).validate_selectors

                rc = _print_validation(validate_selectors(connector))
                return rc

            wa, email = _build_senders(config)
            report = RunReport(live=config.live)
            orch = Orchestrator(
                config=config,
                connector=connector,
                repo=repo,
                optout_repo=optout_repo,
                log_repo=log_repo,
                wa_sender=wa,
                email_sender=email,
                report=report,
                notifier=notifier,
                limit=args.limit,
            )
            try:
                orch.run()
            except CircuitBreaker as err:
                log.error("disjuntor acionado: %s", err)
                rc = 5
            csv_path = report.write_csv(Path("reports"))
            print(report.console_summary(csv_path))
    except Exception as err:  # noqa: BLE001 - topo da pilha
        log.exception("falha fatal: %s", err)
        rc = 1
    finally:
        _release_lock(lock)
        conn.close()
    return rc


def _inspect_module(config):
    """Módulo de inspeção/validação da seguradora ativa (mag | prudential)."""
    if config.insurer == "prudential":
        from .connectors.prudential import inspect_mode
    else:
        from .connectors.mag import inspect_mode
    return inspect_mode


def _run_dashboard(config, port: int) -> int:
    """Sobe o dashboard web local (FastAPI/uvicorn) e abre o navegador."""
    import threading
    import webbrowser

    import uvicorn

    from .dashboard.app import create_app

    init_db(config.db_path)
    app = create_app(config)
    url = f"http://127.0.0.1:{port}"
    print("\n  ╭───────────────────────────────────────────────╮")
    print(f"  │  Régua — painel em {url:<27}│")
    print("  │  escolha a seguradora (MAG/Prudential) no login│")
    print("  │  (Ctrl+C para encerrar)                       │")
    print("  ╰───────────────────────────────────────────────╯\n")
    # Segurança (must-fix #5): o painel tem auth fraca. Se for receber webhooks
    # reais (ngrok), exponha SOMENTE /webhook/* e use uma senha não-vazia.
    if config.zapi_webhook_secret and not config.dashboard_password:
        print("  ⚠️  ATENÇÃO: webhook configurado mas DASHBOARD_PASSWORD está vazio.")
        print("     Antes de expor via ngrok, defina DASHBOARD_PASSWORD e tunele só /webhook/*.\n")

    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0


def _test_whatsapp(config, numero: str) -> int:
    """Checa o status da instância Z-API e envia 1 mensagem de teste."""
    from .messaging.phone import canonical_brazilian_phone
    from .messaging.whatsapp import ZApiClient, ZApiError

    if not (config.zapi_instance_id and config.zapi_token):
        print("Z-API não configurado. Preencha ZAPI_INSTANCE_ID e ZAPI_TOKEN no .env "
              "(CLIENT_TOKEN é opcional).")
        return 2
    destino = (numero or "").strip() or config.whatsapp_override_to or config.notify_whatsapp_to
    phone = canonical_brazilian_phone(destino)
    if not phone:
        print(f"Número inválido: {destino!r}")
        return 2

    client = ZApiClient(config.zapi_instance_id, config.zapi_token, config.zapi_client_token)
    try:
        status = client.get_status()
        print(f"Status da instância: {status}")
    except ZApiError as err:
        print(f"Falha ao consultar status do Z-API: {err}")
        return 1
    if status.get("connected") is False or status.get("needsQrCode") is True:
        print("⚠️  Instância NÃO conectada (precisa parear/QR code). Mensagem não enviada.")
        return 1
    try:
        resp = client.send_text(
            phone,
            "✅ Teste da Régua de Cobrança MAG — conexão Z-API funcionando. "
            "Esta é uma mensagem de teste (fase de testes).",
        )
        print(f"Mensagem de teste enviada para +{phone}. Resposta: "
              f"messageId={resp.get('messageId') or resp.get('id')}")
        return 0
    except ZApiError as err:
        print(f"Falha ao enviar: {err}")
        return 1


def _do_login(config, notifier) -> int:
    """Login humano via Chrome normal, captura de cookies e verificação."""
    if config.insurer == "prudential":
        from .connectors.prudential.login_browser import login_and_capture
    else:
        from .connectors.mag.login_browser import login_and_capture

    rotulo = config.insurer.upper()
    if not login_and_capture(config):
        print(
            "\n⚠️  Não detectei uma sessão válida. Rode `--login` de novo, conclua o "
            "login até CAIR na plataforma e só então pressione ENTER."
        )
        return 3

    # Confirma reabrindo com o Playwright + cookies reinjetados.
    from .connectors.factory import build_connector

    connector = build_connector(config, notifier=notifier)
    try:
        with connector:
            if connector.session.is_authenticated():
                print(f"\n✅ Sessão {rotulo} autenticada e salva. Pode rodar a régua.")
                # Prudential: aproveita a sessão FRESCA (tokens são curtos) para já
                # dumpar o DOM do Relatório de Atraso e calibrar os selectors.
                if config.insurer == "prudential":
                    from .connectors.prudential.inspect_mode import capture_form_dom

                    try:
                        capture_form_dom(connector, Path("artifacts"))
                    except Exception as err:  # noqa: BLE001 - captura é best-effort
                        log.warning("auto-captura de calibração falhou: %s", err)
                return 0
            print(
                "\n⚠️  A sessão foi capturada mas não validou na reabertura. "
                "Tente `--login` de novo."
            )
            return 3
    except Exception as err:  # noqa: BLE001
        log.exception("falha ao verificar sessão: %s", err)
        return 1


def _print_validation(results: list[tuple[str, bool, str]]) -> int:
    ok = sum(1 for _, passed, _ in results if passed)
    print(f"\nValidação de seletores: {ok}/{len(results)} OK\n")
    for key, passed, detail in results:
        mark = "OK " if passed else "FALHA"
        print(f"  [{mark}] {key:35s} {detail}")
    return 0 if ok == len(results) else 6


__all__ = ["main", "parse_args"]
