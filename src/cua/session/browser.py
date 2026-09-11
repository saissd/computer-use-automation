"""Owns the live browser session that automation and humans share.

There is exactly one `BrowserContext` per session and both actors use it. That
is not an implementation convenience — it is the requirement. The brief asks
for a human to take over *the same live session*, and anything that spawns a
fresh context for the operator has quietly failed the requirement while
appearing to satisfy it (cookies, form state, and half-filled wizards all
live in the context).

Running headful is what makes the demo handoff real: when the lease moves to a
human, there is a browser window on screen they can genuinely drive, and the
automation is blocked from touching it by the lease check rather than by
politeness.
"""

import secrets
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from cua.session.lease import SessionLease
from cua.surface.web import WebSurface


class BrowserSession:
    """One live surface, one lease, shared by every actor."""

    def __init__(
        self,
        *,
        headless: bool = True,
        evidence_dir: Path | None = None,
        slow_mo_ms: int = 0,
        viewport: dict[str, int] | None = None,
    ):
        self.session_id = "sess_" + secrets.token_hex(4)
        self.headless = headless
        self.slow_mo_ms = slow_mo_ms
        self.evidence_dir = evidence_dir
        self.viewport = viewport or {"width": 1280, "height": 860}

        self.lease = SessionLease(session_id=self.session_id)
        self._pw: Any = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None
        self.surface: WebSurface | None = None
        self._tracing = False

    async def start(self, *, trace: bool = False) -> "BrowserSession":
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=self.headless, slow_mo=self.slow_mo_ms
        )
        self._context = await self._browser.new_context(viewport=self.viewport)
        if trace and self.evidence_dir:
            await self._context.tracing.start(screenshots=True, snapshots=True)
            self._tracing = True
        self.page = await self._context.new_page()
        self.surface = WebSurface(self.page, evidence_dir=self.evidence_dir)
        return self

    async def login(self, base_url: str, user: str, password: str) -> bool:
        """Establish an application session.

        Kept out of the capability artifact on purpose. Authentication is an
        environment concern with tenant-specific credentials; baking it into a
        recorded flow would mean re-recording per tenant and would put secrets
        one serialization away from disk.
        """
        assert self.page is not None
        await self.page.goto(f"{base_url.rstrip('/')}/login", wait_until="domcontentloaded")
        await self.page.fill("#ctl00_txtUserId", user)
        await self.page.fill("#ctl00_txtPasswd", password)
        await self.page.click("input[value='Sign On']")
        await self.page.wait_for_load_state("domcontentloaded")
        return "login" not in self.page.url

    def reauth_hook(self, base_url: str, user: str, password: str):
        """Returns the coroutine the replay engine calls for `do: reauth`."""

        async def _reauth() -> bool:
            ok = await self.login(base_url, user, password)
            if ok and self.page is not None:
                await self.page.goto(base_url, wait_until="domcontentloaded")
            return ok

        return _reauth

    async def close(self) -> None:
        try:
            if self._tracing and self._context and self.evidence_dir:
                await self._context.tracing.stop(
                    path=str(Path(self.evidence_dir) / "trace.zip")
                )
        finally:
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
            if self._pw:
                await self._pw.stop()

    async def __aenter__(self) -> "BrowserSession":
        return await self.start()

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()
