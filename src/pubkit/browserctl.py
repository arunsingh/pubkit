# Copyright 2026 The pubkit Authors
# SPDX-License-Identifier: Apache-2.0
"""Playwright lifecycle: one browser, many adapters, sessions persisted.

The interactive login flow is the important part. pubkit opens a *visible*
window, the person signs in themselves — password manager, MFA, device
confirmation, whatever the platform demands — and pubkit saves only the
resulting storage_state. It never sees, types or stores a password.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from contextlib import asynccontextmanager

from .core.auth import SessionStore

log = logging.getLogger(__name__)

LOGIN_URLS = {
    "medium": ("https://medium.com/m/signin", "https://medium.com/me/stories/drafts"),
    "substack": ("https://substack.com/sign-in", "https://substack.com/home"),
}


@asynccontextmanager
async def browser_page(platform: str, sessions: SessionStore, *, headless: bool = True):
    from playwright.async_api import async_playwright

    state = sessions.load(platform)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(
            storage_state=state,
            viewport={"width": 1440, "height": 900},
        )
        page = await context.new_page()
        try:
            yield page, _uploader(page)
        finally:
            # Refresh the stored session on the way out: cookies rotate, and a
            # session that silently expires mid-run is a bad afternoon.
            try:
                sessions.save(platform, await context.storage_state())
            except Exception:  # noqa: BLE001
                log.debug("could not refresh %s session", platform)
            await context.close()
            await browser.close()


def _uploader(page):
    async def upload(selector: str, paths: Sequence[str]) -> None:
        await page.set_input_files(selector, list(paths))
    return upload


async def interactive_login(platform: str, sessions: SessionStore, timeout: float = 300.0) -> None:
    from playwright.async_api import async_playwright

    login_url, success_url = LOGIN_URLS.get(platform, ("https://" + platform, "https://" + platform))
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await context.new_page()
        await page.goto(login_url)
        print(f"Sign in to {platform} in the window that opened. Waiting up to {timeout:.0f}s…")
        deadline = asyncio.get_event_loop().time() + timeout
        host = success_url.split("/")[2]
        while asyncio.get_event_loop().time() < deadline:
            if host in page.url and "sign" not in page.url and "login" not in page.url:
                await asyncio.sleep(2)
                sessions.save(platform, await context.storage_state())
                await browser.close()
                return
            await asyncio.sleep(1.5)
        await browser.close()
        raise TimeoutError(f"no {platform} login detected within {timeout:.0f}s")
