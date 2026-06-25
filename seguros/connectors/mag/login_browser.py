"""Login humano em Chrome NORMAL + captura de cookies de sessão.

Por que tudo isso:
- O reCAPTCHA da MAG NÃO passa num navegador controlado por CDP/Playwright
  (fica girando). Então o login é feito num Chrome COMUM, sem cliente CDP ligado.
- A MAG usa cookie de SESSÃO (volátil): ele morre ao fechar o Chrome, então o
  perfil persistente em disco não basta. Depois do login, conectamos via CDP só
  para LER os cookies (o captcha já passou) e salvamos num arquivo; os runs
  seguintes reinjetam esses cookies (``session.py``). As telas internas da
  plataforma não têm captcha, então a automação roda normal lá.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger("seguros.mag.login")


def find_chrome() -> str | None:
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
        base = os.environ.get(env)
        if base:
            cand = Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"
            if cand.exists():
                return str(cand)
    return shutil.which("chrome") or shutil.which("chrome.exe") or shutil.which("google-chrome")


def _cookie_file(config) -> Path:
    return Path(config.user_data_dir).resolve() / "session_cookies.json"


def load_cookies(config) -> list[dict]:
    f = _cookie_file(config)
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def save_cookies(config, cookies: list[dict]) -> None:
    out: list[dict] = []
    horizonte = time.time() + 7 * 24 * 3600  # 7 dias p/ cookies de sessão
    for c in cookies:
        d = dict(c)
        exp = d.get("expires")
        if not exp or exp in (-1, 0):
            d["expires"] = horizonte
        out.append(d)
    _cookie_file(config).write_text(json.dumps(out), encoding="utf-8")


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_cdp_ready(port: int, timeout_s: float = 30.0) -> bool:
    deadline = time.time() + timeout_s
    url = f"http://127.0.0.1:{port}/json/version"
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)  # noqa: S310 - localhost
            return True
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    return False


def login_and_capture(config) -> bool:
    """Abre Chrome normal para login humano e captura os cookies de sessão.

    Retorna True se, após o login, a sessão estiver válida (na plataforma).
    """
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
                config.mag_login_url,
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
        "\n=============== LOGIN MAG (Chrome normal) ===============\n"
        "Abri uma janela do Chrome COMUM — o captcha funciona aqui.\n"
        "  1) Faça login: usuário/CPF, senha e marque 'Não sou um robô'.\n"
        "  2) Espere CAIR na plataforma (plataformadosprodutores.mag.com.br).\n"
        "  3) NÃO feche o Chrome ainda — volte aqui e pressione ENTER.\n"
        "========================================================\n"
    )
    input("Pressione ENTER depois de logar e CAIR na plataforma... ")

    platform_host = urlparse(config.mag_inadimplencias_url).netloc or ""
    ok = False
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            try:
                ctx = browser.contexts[0] if browser.contexts else None
                if ctx is None:
                    print("Não consegui acessar o contexto do Chrome.")
                else:
                    cookies = ctx.cookies()
                    save_cookies(config, cookies)
                    ok = _verify_on_platform(ctx, config, platform_host)
                    if ok:
                        # recaptura após confirmar (garante cookies pós-redirect)
                        save_cookies(config, ctx.cookies())
            finally:
                browser.close()  # desconecta o CDP (não mata o Chrome)
    except Exception as err:  # noqa: BLE001
        log.exception("falha ao capturar cookies: %s", err)
        print(f"Erro ao capturar a sessão: {err}")
    finally:
        _terminate(proc)

    return ok


def _verify_on_platform(ctx, config, platform_host: str) -> bool:
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    try:
        page.goto(config.mag_inadimplencias_url, wait_until="commit")
    except Exception:  # noqa: BLE001
        pass
    try:
        page.wait_for_url(lambda u: "identidade.mag.com.br" in (urlparse(u).netloc or ""),
                          timeout=10000)
        return False  # redirecionou para o login => não autenticado
    except Exception:  # noqa: BLE001
        pass
    return bool(platform_host) and platform_host in (urlparse(page.url).netloc or "")


def _terminate(proc) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


__all__ = ["login_and_capture", "load_cookies", "save_cookies", "find_chrome"]
