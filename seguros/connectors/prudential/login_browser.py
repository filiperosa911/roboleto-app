"""Login humano Prudential (Chrome NORMAL) + captura de cookies de sessão.

Mesma mecânica da MAG (o OTP/2FA só funciona no Chrome comum). Reusa os helpers
de baixo nível da MAG (achar o Chrome, porta livre, CDP) e os utilitários de
cookie. Depois do login, capturamos os cookies via CDP e validamos que a sessão
abre o relatório de atraso.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from ..mag.login_browser import (
    _free_port,
    _terminate,
    _wait_cdp_ready,
    find_chrome,
    save_cookies,
)
from .session import is_login_host

log = logging.getLogger("seguros.prudential.login")


def login_and_capture(config) -> bool:
    """Abre Chrome normal para login humano e captura os cookies de sessão."""
    from playwright.sync_api import sync_playwright

    chrome = find_chrome()
    if not chrome:
        print(
            "Não encontrei o chrome.exe. Instale o Google Chrome.\n"
            "Caminho típico: C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
        )
        return False

    user_dir = Path(config.user_data_dir).resolve()
    user_dir.mkdir(parents=True, exist_ok=True)
    port = _free_port()

    try:
        proc = subprocess.Popen(
            [
                chrome,
                f"--user-data-dir={user_dir}",
                f"--remote-debugging-port={port}",
                "--no-first-run",
                "--no-default-browser-check",
                config.prudential_login_url,
            ]
        )
    except OSError as err:
        print(f"Falha ao abrir o Chrome: {err}")
        return False

    if not _wait_cdp_ready(port):
        print("O Chrome não respondeu a tempo. Tente de novo.")
        _terminate(proc)
        return False

    print(
        "\n=============== LOGIN PRUDENTIAL (Chrome normal) ===============\n"
        "Abri uma janela do Chrome COMUM — o OTP/2FA funciona aqui.\n"
        "  1) Faça login: usuário, senha e o OTP do seu app/contato.\n"
        "  2) Espere CAIR na plataforma (Life Planner, logado).\n"
        "  3) NÃO feche o Chrome ainda — volte aqui e pressione ENTER.\n"
        "===============================================================\n"
    )
    input("Pressione ENTER depois de logar e CAIR na plataforma... ")

    ok = False
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            try:
                ctx = browser.contexts[0] if browser.contexts else None
                if ctx is None:
                    print("Não consegui acessar o contexto do Chrome.")
                else:
                    save_cookies(config, ctx.cookies())
                    ok = _verify_on_platform(ctx, config)
                    if ok:
                        save_cookies(config, ctx.cookies())
            finally:
                browser.close()  # desconecta o CDP (não mata o Chrome)
    except Exception as err:  # noqa: BLE001
        log.exception("falha ao capturar cookies: %s", err)
        print(f"Erro ao capturar a sessão: {err}")
    finally:
        _terminate(proc)

    return ok


def _verify_on_platform(ctx, config) -> bool:
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    try:
        page.goto(config.prudential_atraso_url, wait_until="commit")
    except Exception:  # noqa: BLE001
        pass
    try:
        page.wait_for_url(is_login_host, timeout=10000)
        return False  # redirecionou para o SSO => não autenticado
    except Exception:  # noqa: BLE001
        pass
    return "prudential.com.br" in (urlparse(page.url).netloc or "").lower()


__all__ = ["login_and_capture"]
