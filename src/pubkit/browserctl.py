# Copyright 2026 The pubkit Authors
# SPDX-License-Identifier: Apache-2.0
"""Browser lifecycle.

Browser adapters need a live Playwright page and a way to hand real bytes to a
file input. Nothing else in the pipeline should have to know that, so this
module owns it: one browser for the whole run, one context per platform, and
sessions refreshed on the way out.

The login flow is deliberately human-in-the-loop:

    pubkit auth login medium
      → a *visible* window opens at the platform's login page
      → you sign in: password manager, MFA, device confirmation, whatever
      → pubkit waits until it sees you are through, saves the session, closes

pubkit sees cookies. It never sees, types or stores a password. That is not
only the right security posture, it is the only thing that works: platforms
increasingly gate login behind challenges an automation layer has no business
trying to defeat.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from contextlib import AsyncExitStack, asynccontextmanager

from .core.auth import CredentialError, SessionStore

log = logging.getLogger(__name__)

#: (where to send the user, how to tell they made it)
LOGIN_FLOWS: dict[str, tuple[str, str]] = {
    "medium": ("https://medium.com/m/signin", "medium.com/me/"),
    "substack": ("https://substack.com/sign-in", "substack.com/home"),
}

#: Platforms whose adapters need a page injected.
BROWSER_PLATFORMS = set(LOGIN_FLOWS)


def _uploader(page):
    """Hand real bytes to a file input.

    Never click a file input to open a picker — a native dialog cannot be
    driven and it blocks the whole session. `set_input_files` sets the files
    directly, which is why `BrowserAdapter` injects its own hidden input.
    """

    async def upload(selector: str, paths: Sequence[str]) -> None:
        await page.set_input_files(selector, list(paths))

    return upload


class BrowserPool:
    """One browser for the run; one context per platform.

    Separate contexts matter: each platform gets only its own cookies, so a
    Medium session is never presented to Substack.
    """

    def __init__(self, sessions: SessionStore, *, headless: bool = True, slow_mo: int = 0) -> None:
        self.sessions = sessions
        self.headless = headless
        self.slow_mo = slow_mo
        self._stack = AsyncExitStack()
        self._pw = None
        self._browser = None
        self._contexts: dict[str, object] = {}

    async def __aenter__(self) -> BrowserPool:
        from playwright.async_api import async_playwright

        self._pw = await self._stack.enter_async_context(async_playwright())
        self._browser = await self._pw.chromium.launch(headless=self.headless, slow_mo=self.slow_mo)
        return self

    async def __aexit__(self, *exc) -> None:
        for platform, ctx in self._contexts.items():
            # Cookies rotate. Refreshing on the way out is what stops a session
            # silently expiring between runs.
            try:
                self.sessions.save(platform, await ctx.storage_state())
            except Exception:  # noqa: BLE001
                log.debug("could not refresh the %s session", platform)
            try:
                await ctx.close()
            except Exception:  # noqa: BLE001
                pass
        if self._browser:
            await self._browser.close()
        await self._stack.aclose()

    async def page_for(self, platform: str):
        """A page carrying `platform`'s saved session, plus its uploader."""
        if platform in self._contexts:
            ctx = self._contexts[platform]
            return ctx.pages[0], _uploader(ctx.pages[0])

        state = self.sessions.load(platform)
        if state is None:
            raise CredentialError(
                f"no saved {platform} session.\n"
                f"  Run:  pubkit auth login {platform}\n"
                f"  A browser window opens, you sign in yourself, and pubkit keeps\n"
                f"  only the session — it never asks for your password."
            )
        ctx = await self._browser.new_context(
            storage_state=state, viewport={"width": 1440, "height": 900}
        )
        page = await ctx.new_page()
        self._contexts[platform] = ctx
        return page, _uploader(page)


@asynccontextmanager
async def attached(adapters: Sequence, sessions: SessionStore, *, headless: bool = True):
    """Yield `adapters` with browser ones wired to a live page.

    This is the seam that was missing: the runner calls `adapter.authenticate()`
    and friends, but a `MediumAdapter` built by the registry has `_page = None`
    until something puts a page in it. That something is here.

    API adapters pass through untouched, and no browser is launched at all if
    none of the adapters need one — publishing to Dev.to should not pay for
    Chromium.
    """
    needs_browser = [a for a in adapters if getattr(a, "name", None) in BROWSER_PLATFORMS]
    if not needs_browser:
        yield list(adapters)
        return

    # Check every session BEFORE a browser exists. A missing login is the most
    # common reason a publish cannot start, and spending a Chromium launch to
    # then say "run pubkit auth login" is both slow and the wrong order — it
    # also leaves a browser process to clean up on the failure path.
    missing = [a.name for a in needs_browser if sessions.load(a.name) is None]
    if missing:
        raise CredentialError(
            "no saved session for: " + ", ".join(missing) + ".\n"
            + "\n".join(f"  Run:  pubkit auth login {p}" for p in missing)
            + "\n  A browser window opens, you sign in yourself, and pubkit keeps\n"
            "  only the session — it never asks for your password."
        )

    async with BrowserPool(sessions, headless=headless) as pool:
        for adapter in needs_browser:
            page, upload = await pool.page_for(adapter.name)
            adapter._page = page
            adapter._upload = upload
            log.debug("attached a browser page to %s", adapter.name)
        yield list(adapters)


async def interactive_login(platform: str, sessions: SessionStore, timeout: float = 300.0) -> None:
    """Open a visible window, wait for the human, save the session."""
    from playwright.async_api import async_playwright

    login_url, success_marker = LOGIN_FLOWS.get(
        platform, (f"https://{platform}.com/login", f"{platform}.com")
    )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        ctx = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await ctx.new_page()
        await page.goto(login_url)

        print(f"\n  A browser window is open at {login_url}")
        print("  Sign in there — password manager, MFA, all of it.")
        print(f"  Waiting up to {timeout / 60:.0f} minutes. Close this with Ctrl-C to cancel.\n")

        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                url = page.url
            except Exception as exc:  # window closed by the user
                await browser.close()
                raise CredentialError(
                    f"{platform} login window was closed before sign-in completed"
                ) from exc

            if success_marker in url:
                await asyncio.sleep(2)  # let the last auth cookie land
                sessions.save(platform, await ctx.storage_state())
                await browser.close()
                return
            await asyncio.sleep(1.5)

        await browser.close()
        raise TimeoutError(
            f"no {platform} sign-in detected within {timeout / 60:.0f} minutes. "
            f"pubkit watches for a URL containing {success_marker!r}."
        )


async def verify_session(platform: str, sessions: SessionStore, *, headless: bool = True) -> bool:
    """Is the saved session still good? Cheaper to find out now than mid-publish."""
    _, success_marker = LOGIN_FLOWS.get(platform, ("", platform))
    probe = {
        "medium": "https://medium.com/me/stories/drafts",
        "substack": "https://substack.com/home",
    }.get(platform)
    if probe is None or sessions.load(platform) is None:
        return False

    async with BrowserPool(sessions, headless=headless) as pool:
        page, _ = await pool.page_for(platform)
        await page.goto(probe, wait_until="domcontentloaded")
        await asyncio.sleep(1.5)
        return "signin" not in page.url and "sign-in" not in page.url
