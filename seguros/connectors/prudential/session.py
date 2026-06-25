"""Sessão Playwright da Prudential: contexto persistente + detecção de login.

Mesma filosofia da MAG: sem quebra de captcha/OTP. O humano loga UMA vez no
``--login`` (Chrome normal), capturamos os cookies (``.prudential.com.br`` cobre
o ASPX em ``saa.prudential.com.br``) e reinjetamos nos runs seguintes. A detecção
de "autenticado" é por URL (independente de seletor): se cair no SSO, deslogou.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from playwright.sync_api import (
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeout,
    sync_playwright,
)

from ..base import NotAuthenticatedError, SessionExpiredError
from ..mag.login_browser import load_cookies
from .selectors import SelectorConfig

log = logging.getLogger("seguros.prudential.session")

# Hosts do SSO/login da Prudential (cair aqui = NÃO autenticado).
_LOGIN_HOST_HINTS = ("pob-sso.prudential.com.br", "sso.prudential", "login.prudential")


def is_login_host(url: str) -> bool:
    try:
        netloc = (urlparse(url).netloc or "").lower()
    except Exception:  # noqa: BLE001
        return False
    return any(h in netloc for h in _LOGIN_HOST_HINTS)


class PrudentialSession:
    def __init__(
        self,
        config,
        selectors: SelectorConfig,
        *,
        notifier=None,
        default_timeout_ms: int = 15000,
        nav_timeout_ms: int = 45000,
        auth_probe_timeout_ms: int = 12000,
    ) -> None:
        self.cfg = config
        self.sel = selectors
        self.notifier = notifier
        self.default_timeout_ms = default_timeout_ms
        self.nav_timeout_ms = nav_timeout_ms
        self.auth_probe_timeout_ms = auth_probe_timeout_ms
        # host da plataforma (relatório ASPX) — base da detecção de auth por URL.
        self._platform_host = urlparse(config.prudential_atraso_url).netloc or ""
        self._pw = None
        self._ctx = None
        self.page = None

    # --- ciclo de vida -------------------------------------------------------

    def start(self) -> None:
        self.cfg.user_data_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.cfg.user_data_dir),
            headless=False,
            channel="chrome",
            locale="pt-BR",
            timezone_id=self.cfg.timezone,
            no_viewport=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
                "--start-maximized",
            ],
            ignore_default_args=["--enable-automation"],
        )
        self._ctx.set_default_timeout(self.default_timeout_ms)
        self._ctx.set_default_navigation_timeout(self.nav_timeout_ms)
        self._restore_cookies()
        self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()

    def _restore_cookies(self) -> None:
        cookies = load_cookies(self.cfg)
        if not cookies:
            return
        try:
            self._ctx.add_cookies(cookies)
            log.info("cookies de sessão Prudential reinjetados (%d)", len(cookies))
        except PlaywrightError as err:
            log.warning("falha ao reinjetar cookies: %s", err)

    def close(self) -> None:
        try:
            if self._ctx is not None:
                self._ctx.close()
        finally:
            if self._pw is not None:
                self._pw.stop()
            self._ctx = None
            self._pw = None
            self.page = None

    # --- autenticação --------------------------------------------------------

    def _authenticated_by_url(self, url: str) -> bool:
        if is_login_host(url):
            return False
        netloc = (urlparse(url).netloc or "").lower()
        # autenticado = dentro do domínio prudential e fora do SSO
        return "prudential.com.br" in netloc

    def is_authenticated(self) -> bool:
        # Navega ATÉ ASSENTAR e checa a URL FINAL. Importante: NÃO concluímos
        # "deslogado" por um bounce transitório no SSO durante o carregamento —
        # a Prudential, mesmo LOGADA, pode passar de raspão pelo pob-sso antes de
        # renderizar o relatório. Só a URL final (saa autenticado vs SSO) decide.
        try:
            self.page.goto(
                self.cfg.prudential_atraso_url,
                wait_until="domcontentloaded",
                timeout=self.nav_timeout_ms,
            )
        except (PlaywrightTimeout, PlaywrightError):
            pass
        try:
            self.page.wait_for_timeout(3000)  # deixa eventual redirect AEM/SSO assentar
        except PlaywrightError:
            pass
        return self._authenticated_by_url(self.page.url)

    def ensure_authenticated(self, *, interactive: bool) -> None:
        if self.is_authenticated():
            log.info("sessão Prudential válida")
            return
        # Tokens da Prudential são CURTOS (minutos): login e operação têm que ser
        # na MESMA janela/sessão, senão o cookie salvo expira no meio. No modo
        # interativo (CLI), pausamos para o humano logar AQUI e seguimos na hora.
        # CLI (interactive): pausa com ENTER. Dashboard (sem terminal): espera por
        # POLLING o humano logar na MESMA janela headed que o worker abriu.
        if interactive and self._prompt_login():
            log.info("login Prudential confirmado na sessão (CLI)")
            return
        if not interactive and self._wait_for_login():
            log.info("login Prudential confirmado na sessão (painel)")
            return
        msg = (
            "Sessão Prudential não autenticada. Faça o login (usuário, senha e OTP) "
            "NA JANELA do Chrome que abriu, até CAIR na plataforma, e tente de novo."
        )
        if self.notifier:
            self.notifier.notify(msg)
        raise NotAuthenticatedError(msg)

    def _prompt_login(self) -> bool:
        """Login humano na MESMA janela Playwright (sessão é curta → sem gap de
        cookie). Só funciona com terminal interativo (CLI)."""
        try:
            self.page.goto(
                self.cfg.prudential_atraso_url,
                wait_until="domcontentloaded",
                timeout=self.nav_timeout_ms,
            )
        except (PlaywrightTimeout, PlaywrightError):
            pass
        print(
            "\n=============== LOGIN PRUDENTIAL ===============\n"
            "Abri o Relatório de Atraso numa janela do Chrome.\n"
            "  1) Faça login (usuário, senha e OTP) NESSA janela.\n"
            "  2) Espere CAIR no relatório (logado).\n"
            "  3) Volte aqui e pressione ENTER.\n"
            "===============================================\n"
        )
        try:
            input("Pressione ENTER depois de logar... ")
        except EOFError:
            return False
        ok = self.is_authenticated()
        if ok:
            self._save_cookies()
        return ok

    def _wait_for_login(self, *, max_polls: int = 50, poll_ms: int = 3000) -> bool:
        """Dashboard (sem terminal): abre o relatório (cai no login) e espera por
        POLLING o humano logar na janela headed. Sem `input()` — detecta sozinho
        quando a URL vira a do relatório autenticado (até ~2,5 min)."""
        try:
            self.page.goto(
                self.cfg.prudential_atraso_url,
                wait_until="domcontentloaded",
                timeout=self.nav_timeout_ms,
            )
        except (PlaywrightTimeout, PlaywrightError):
            pass
        log.info("Prudential: aguardando login humano na janela (até ~%ds)...",
                 max_polls * poll_ms // 1000)
        for _ in range(max_polls):
            if self._authenticated_by_url(self.page.url):
                self._save_cookies()
                return True
            try:
                self.page.wait_for_timeout(poll_ms)
            except PlaywrightError:
                break
        return False

    def _save_cookies(self) -> None:
        try:
            from ..mag.login_browser import save_cookies

            save_cookies(self.cfg, self._ctx.cookies())
        except Exception as err:  # noqa: BLE001 - best-effort
            log.debug("falha ao salvar cookies pós-login: %s", err)

    # --- navegação guarda-costas --------------------------------------------

    def goto(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded")
        if is_login_host(self.page.url):
            raise SessionExpiredError("redirecionado para o SSO da Prudential")


__all__ = ["PrudentialSession", "is_login_host"]
