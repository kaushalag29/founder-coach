"""Rendering JavaScript pages with Playwright (headless Chromium), only when needed (plan D3).

One browser per run, started on first use and reused. Images, media and fonts aren't
downloaded (the text is what we need). If Playwright or its browser isn't installed, the
renderer reports itself unavailable once and the crawl carries on with plain HTTP.
"""
from __future__ import annotations

from ..config import WEB_RENDER_TIMEOUT_S, WEB_USER_AGENT

BLOCKED = {"image", "media", "font"}
NETWORK_IDLE_MAX_MS = 10_000         # wait for late XHR content, but never longer than this


class RenderError(RuntimeError):
    pass


class Renderer:
    def __init__(self, user_agent: str = WEB_USER_AGENT, timeout_s: float = WEB_RENDER_TIMEOUT_S):
        self.user_agent, self.timeout_ms = user_agent, int(timeout_s * 1000)
        self._pw = self._browser = self._context = None
        self.unavailable: str | None = None

    def available(self) -> bool:
        if self._context is not None:
            return True
        if self.unavailable:
            return False
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.unavailable = ("Playwright isn't installed: `uv pip install -e \".[web]\"` then "
                                "`playwright install chromium`; JavaScript pages use plain HTTP until then")
            return False
        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
            self._context = self._browser.new_context(user_agent=self.user_agent, java_script_enabled=True,
                                                      service_workers="block")
            self._context.route("**/*", lambda route: route.abort() if route.request.resource_type in BLOCKED
                                else route.continue_())
        except Exception as e:                    # noqa: BLE001 -- no browser binary, sandbox limits, ...
            self.unavailable = (f"the browser didn't start ({type(e).__name__}: {str(e).splitlines()[0][:160]}); "
                                "`playwright install chromium` usually fixes it")
            self.close()
            return False
        return True

    def render(self, url: str) -> tuple[str, str, int]:
        """(html, final url, HTTP status) of the page after its scripts ran."""
        if not self.available():
            raise RenderError(self.unavailable or "renderer unavailable")
        from playwright.sync_api import Error as PWError, TimeoutError as PWTimeout
        page = self._context.new_page()
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=min(NETWORK_IDLE_MAX_MS, self.timeout_ms))
            except PWTimeout:
                pass                              # a page that keeps polling still has its content by now
            return page.content(), page.url, (resp.status if resp else 0)
        except PWTimeout:
            raise RenderError(f"timed out after {self.timeout_ms // 1000}s") from None
        except PWError as e:
            raise RenderError(f"{type(e).__name__}: {str(e).splitlines()[0][:200]}") from None
        finally:
            try:
                page.close()
            except Exception:                     # noqa: BLE001 -- closing a crashed page
                pass

    def close(self) -> None:
        for obj, meth in ((self._context, "close"), (self._browser, "close"), (self._pw, "stop")):
            try:
                if obj is not None:
                    getattr(obj, meth)()
            except Exception:                     # noqa: BLE001
                pass
        self._pw = self._browser = self._context = None
