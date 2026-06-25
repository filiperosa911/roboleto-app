"""Sessão Playwright: contexto persistente, detecção de login e login humano.

Sem quebra de captcha (decisão de projeto): o captcha só aparece no login. Com
contexto persistente, o humano resolve UMA vez e a sessão sobrevive entre runs;
quando expira (raro), notificamos e (modo interativo) pausamos para o login.
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
from .selectors import SelectorConfig

log = logging.getLogger("seguros.mag.session")

_LOGIN_HOST_HINT = "identidade.mag.com.br"


class MagSession:
    def __init__(
        self,
        config,
        selectors: SelectorConfig,
        *,
        notifier=None,
        default_timeout_ms: int = 15000,
        nav_timeout_ms: int = 45000,
        auth_probe_timeout_ms: int = 12000,
        login_wait_max_s: int = 300,
    ) -> None:
        self.cfg = config
        self.sel = selectors
        self.notifier = notifier
        self.default_timeout_ms = default_timeout_ms
        self.nav_timeout_ms = nav_timeout_ms
        self.auth_probe_timeout_ms = auth_probe_timeout_ms
        self.login_wait_max_s = login_wait_max_s
        # host da plataforma (autenticada) extraído da URL de inadimplências —
        # detectar login por URL é calibração-independente (não depende de seletor).
        self._platform_host = urlparse(config.mag_inadimplencias_url).netloc or ""
        self._pw = None
        self._ctx = None
        self.page = None

    # --- ciclo de vida -------------------------------------------------------

    def start(self) -> None:
        self.cfg.user_data_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        # Apresenta o Chrome como um navegador normal (sem a barra/flag de
        # automação) — alguns servidores de identidade renderizam o formulário
        # pela metade no modo automação. NÃO contorna o captcha: o login é humano.
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
        # Esconde overlays (Hotjar/pesquisa) que INTERCEPTAM cliques — chegavam a
        # bloquear o botão "próximo" da paginação e o checkbox/Cobrar.
        try:
            self._ctx.add_init_script(
                "const s=document.createElement('style');"
                "s.textContent='._hj-widget-container,._hj_feedback_container,"
                "[id^=\"_hj\"],[id^=\"survey_\"],[class*=\"hj-widget\"]"
                "{display:none!important;pointer-events:none!important;}';"
                "(document.documentElement||document.head).appendChild(s);"
            )
        except PlaywrightError:
            pass
        try:
            self._ctx.grant_permissions(["clipboard-read", "clipboard-write"])
        except PlaywrightError:
            pass
        # Reinjeta os cookies de sessão capturados no --login (a MAG usa cookie
        # volátil que não sobrevive em disco — ver login_browser.py).
        self._restore_cookies()
        self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()

    def _restore_cookies(self) -> None:
        from .login_browser import load_cookies

        cookies = load_cookies(self.cfg)
        if not cookies:
            return
        try:
            self._ctx.add_cookies(cookies)
            log.info("cookies de sessão reinjetados (%d)", len(cookies))
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

    def _on_login_host(self) -> bool:
        return self._host_is_login(self.page.url)

    @staticmethod
    def _host_is_login(url: str) -> bool:
        try:
            return _LOGIN_HOST_HINT in (urlparse(url).netloc or "")
        except Exception:  # noqa: BLE001
            return False

    def _authenticated_by_url(self, url: str) -> bool:
        """Autenticado = fora do host de login e dentro do host da plataforma.

        Calibração-independente: não depende de nenhum seletor do ``selectors.yaml``.
        """
        if self._host_is_login(url):
            return False
        netloc = urlparse(url).netloc or ""
        return bool(self._platform_host) and self._platform_host in netloc

    def is_authenticated(self) -> bool:
        try:
            self.page.goto(self.cfg.mag_inadimplencias_url, wait_until="commit")
        except (PlaywrightTimeout, PlaywrightError):
            pass
        # IMPORTANTE: deslogado, o SPA carrega PRIMEIRO no host da plataforma e
        # SÓ DEPOIS redireciona (client-side) para o login. Por isso esperamos
        # ver se há redirect para o host de login antes de concluir.
        try:
            self.page.wait_for_url(self._host_is_login, timeout=self.auth_probe_timeout_ms)
            return False  # redirecionou para o login => NÃO autenticado
        except (PlaywrightTimeout, PlaywrightError):
            pass
        return self._authenticated_by_url(self.page.url)

    def ensure_authenticated(self, *, interactive: bool) -> None:
        # O login humano NÃO é feito aqui (o reCAPTCHA não passa no navegador
        # automatizado). Ele é feito no `--login` via Chrome normal; aqui só
        # verificamos se a sessão persistida está válida.
        if self.is_authenticated():
            log.info("sessão MAG válida")
            return
        msg = (
            "Sessão MAG não autenticada. Rode `python -m seguros --login` e faça o "
            "login (o captcha só funciona no Chrome normal do --login)."
        )
        if self.notifier:
            self.notifier.notify(msg)
        raise NotAuthenticatedError(msg)

    # --- navegação guarda-costas --------------------------------------------

    def goto(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded")
        self._assert_still_authenticated()
        self._recover_from_aura_error()

    def _recover_from_aura_error(self) -> None:
        """Recupera do overlay 'Sorry to interrupt / CSS Error' do Salesforce
        (falha de carregamento de recurso) recarregando a página."""
        for _ in range(2):
            try:
                marker = self.page.get_by_text("Sorry to interrupt")
                marker.first.wait_for(state="visible", timeout=1500)
            except (PlaywrightTimeout, PlaywrightError):
                return  # sem erro
            log.warning("overlay de erro do Salesforce detectado — recarregando")
            try:
                self.page.get_by_role("button", name="Refresh").first.click(timeout=3000)
            except (PlaywrightTimeout, PlaywrightError):
                try:
                    self.page.reload(wait_until="domcontentloaded")
                except (PlaywrightTimeout, PlaywrightError):
                    return
            try:
                self.page.wait_for_timeout(3000)
            except PlaywrightError:
                return

    def _assert_still_authenticated(self) -> None:
        # Checagem por URL (calibração-independente): se fomos redirecionados ao
        # host de login, a sessão caiu.
        if self._host_is_login(self.page.url):
            raise SessionExpiredError("redirecionado para a tela de login")

    def touch_session(self) -> None:
        """Refresca a sessão visitando uma página autenticada (vida da sessão)."""
        try:
            self.goto(self.cfg.mag_inadimplencias_url)
        except (PlaywrightTimeout, SessionExpiredError):
            pass


__all__ = ["MagSession"]
