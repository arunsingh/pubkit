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
import os
from collections.abc import Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from .core.auth import CredentialError, SessionStore
from .core.login import (
    FLOWS,
    Finding,
    Observation,
    is_challenge_script,
    looks_signed_in,
    validate_probe,
    validate_state,
    worst,
)

log = logging.getLogger(__name__)

#: Platforms whose adapters need a page injected. One source of truth: a
#: platform has a browser login flow, therefore its adapter needs a page.
BROWSER_PLATFORMS = set(FLOWS)


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


# --------------------------------------------------------------------- login
#
# Three ways to get a window the human can sign in to, in the order they are
# tried. The order is not arbitrary: it goes from "a browser we launched" to
# "the browser you already use", because that is also the order of how likely a
# platform is to let the sign-in finish.
#
#   1. real Google Chrome, driven by Playwright, with a persistent pubkit
#      profile — a profile that accumulates history and cookies across runs
#      rather than looking brand new every time
#   2. the Chromium that ships with Playwright, same persistent profile
#   3. --attach: connect to a Chrome the user started themselves. pubkit drives
#      nothing about the sign-in; it watches a tab the user is already in.
#
# What pubkit will not do is pretend to be something it is not. If a platform
# declines to complete a sign-in in a launched window, --attach is the answer,
# because then it genuinely is the user's own browser.

DEFAULT_CDP = "http://localhost:9222"


def profile_dir(platform: str) -> Path:
    # Overridable so tests never touch a real home directory.
    root = Path(os.environ.get("PUBKIT_PROFILE_DIR", Path.home() / ".pubkit" / "profiles"))
    d = root / platform
    d.mkdir(parents=True, exist_ok=True)
    return d


async def _open_login_context(pw, platform: str, *, attach: str | None, headless: bool = False):
    """Return (context, close_fn, how). See the note above for the order."""
    if attach:
        browser = await pw.chromium.connect_over_cdp(attach)
        if not browser.contexts:
            raise CredentialError(
                f"connected to {attach} but that Chrome has no window open. "
                "Open a tab and try again."
            )
        ctx = browser.contexts[0]

        async def close():
            # Never close a browser we did not start — it is the user's.
            await browser.close()

        return ctx, close, f"attached to your Chrome at {attach}"

    args = {
        "user_data_dir": str(profile_dir(platform)),
        "headless": headless,
        "viewport": {"width": 1280, "height": 900},
        # Drops the "controlled by automated software" infobar, which steals a
        # strip of the window the user is trying to type in.
        "ignore_default_args": ["--enable-automation"],
    }
    try:
        ctx = await pw.chromium.launch_persistent_context(channel="chrome", **args)
        how = "Google Chrome, pubkit profile"
    except Exception:  # noqa: BLE001 - Chrome simply may not be installed
        ctx = await pw.chromium.launch_persistent_context(**args)
        how = "bundled Chromium, pubkit profile"

    async def close():
        await ctx.close()

    return ctx, close, how


def _watch(page, obs: Observation) -> None:
    """Record everything that could explain a button that does nothing."""

    def on_console(m):
        if m.type in ("error", "warning"):
            obs.console_errors.append(f"{m.type}: {m.text}")

    def on_failed(r):
        obs.failed_requests.append(f"{r.method} {r.url[:120]} :: {r.failure}")

    def on_request(r):
        if is_challenge_script(r.url):
            obs.challenge_scripts.append(r.url)
        # A sign-in is a POST (or a GraphQL call) to the platform's own host.
        # Counting them is what separates "you did not finish" from "the button
        # is dead", which are the same silence from outside.
        if r.method in ("POST", "PUT") and r.resource_type in ("xhr", "fetch", "document"):
            obs.submits += 1

    page.on("console", on_console)
    page.on("requestfailed", on_failed)
    page.on("request", on_request)


async def interactive_login(
    platform: str,
    sessions: SessionStore,
    timeout: float = 300.0,
    *,
    attach: str | None = None,
    headless: bool = False,
    report: Callable[[str], None] = print,
) -> list[Finding]:
    """Open a window, wait for the human, prove the session, then save it.

    Returns every finding from all three phases. The session is written only
    after the post-flight probe passes: a saved session that does not work is
    worse than none, because it defers the failure to the middle of a publish.
    """
    from playwright.async_api import async_playwright

    flow = FLOWS.get(platform)
    if flow is None:
        raise CredentialError(
            f"{platform} has no browser login flow. "
            f"Known: {', '.join(sorted(FLOWS))}. API platforms use `pubkit auth login {platform}` "
            "with a token instead."
        )

    findings: list[Finding] = []
    obs = Observation()

    async with async_playwright() as pw:
        ctx, close, how = await _open_login_context(pw, platform, attach=attach, headless=headless)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        _watch(page, obs)

        try:
            await page.goto(flow.login_url, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            await close()
            findings.append(
                Finding(
                    "reachable",
                    "fail",
                    f"could not open {flow.login_url}: {str(exc)[:120]}",
                    "check your network, VPN or proxy",
                )
            )
            return findings

        report(f"\n  Window open ({how})")
        report(f"  Sign in at {flow.login_url} — password manager, MFA, all of it.")
        report("  pubkit never sees your password. It is watching for the session only.")
        report(f"  Waiting up to {timeout / 60:.0f} minutes; Ctrl-C cancels.\n")

        deadline = asyncio.get_event_loop().time() + timeout
        last_report = 0.0
        state: dict | None = None

        while asyncio.get_event_loop().time() < deadline:
            try:
                url = page.url
                cookies = await ctx.cookies()
            except Exception as exc:  # noqa: BLE001 - the user closed the window
                await close()
                findings.append(
                    Finding(
                        "window",
                        "fail",
                        "closed before the sign-in completed",
                        str(exc)[:100],
                    )
                )
                findings += obs.diagnose(flow)
                return findings

            obs.note_url(url)
            names = {c["name"] for c in cookies if flow.cookie_domain in c.get("domain", "")}

            if looks_signed_in(flow, url, names):
                await asyncio.sleep(2)  # let the last auth cookie land
                state = await ctx.storage_state()
                break

            # Silence for five minutes is its own bug report. Say what is being
            # waited for, so the user can tell pubkit is watching the right thing.
            now = asyncio.get_event_loop().time()
            if now - last_report > 30:
                last_report = now
                have = ", ".join(sorted(names & set(flow.required_cookies))) or "none yet"
                report(f"    … still waiting. at {url[:70]} · cookies: {have}")

            await asyncio.sleep(1.5)

        if state is None:
            await close()
            findings.append(
                Finding("timeout", "fail", f"no sign-in seen in {timeout / 60:.0f} minutes")
            )
            findings += obs.diagnose(flow)
            return findings

        # --------------------------------------------------- post-flight
        findings += validate_state(flow, state)
        if worst(findings) == "fail":
            await close()
            findings.append(
                Finding("saved", "fail", "nothing was written", "fix the above and sign in again")
            )
            return findings

        probe_page = await ctx.new_page()
        try:
            resp = await probe_page.goto(flow.probe_url, wait_until="domcontentloaded")
            await asyncio.sleep(1.5)
            body = await probe_page.evaluate("() => document.body.innerText.slice(0, 4000)")
            findings.append(
                validate_probe(
                    flow,
                    final_url=probe_page.url,
                    body_text=body,
                    status=resp.status if resp else None,
                )
            )
            state = await ctx.storage_state()
        except Exception as exc:  # noqa: BLE001
            findings.append(
                Finding("probe", "fail", f"could not load {flow.probe_url}: {str(exc)[:100]}")
            )
        finally:
            await probe_page.close()

        if worst(findings) == "fail":
            await close()
            findings.append(Finding("saved", "fail", "nothing was written"))
            return findings

        sessions.save(platform, state)
        findings.append(Finding("saved", "ok", f"{platform} session stored, encrypted at rest"))
        await close()

    return findings


async def verify_session(platform: str, sessions: SessionStore, *, headless: bool = True) -> bool:
    """Is the saved session still good? Cheaper to find out now than mid-publish."""
    return worst(await verify_session_detail(platform, sessions, headless=headless)) != "fail"


async def verify_session_detail(
    platform: str, sessions: SessionStore, *, headless: bool = True
) -> list[Finding]:
    """The same three post-flight checks `login` runs, on a session already saved.

    Same code path as login's own verification — a session cannot pass one and
    fail the other, which is the property that makes `verify` worth trusting.
    """
    flow = FLOWS.get(platform)
    if flow is None:
        return [Finding("platform", "fail", f"{platform} has no browser login flow")]

    state = sessions.load(platform)
    if state is None:
        return [
            Finding("session", "fail", "none saved", f"pubkit auth login {platform}")
        ]

    findings = validate_state(flow, state)
    if worst(findings) == "fail":
        return findings

    async with BrowserPool(sessions, headless=headless) as pool:
        page, _ = await pool.page_for(platform)
        try:
            resp = await page.goto(flow.probe_url, wait_until="domcontentloaded")
            await asyncio.sleep(1.5)
            body = await page.evaluate("() => document.body.innerText.slice(0, 4000)")
            findings.append(
                validate_probe(
                    flow, final_url=page.url, body_text=body, status=resp.status if resp else None
                )
            )
        except Exception as exc:  # noqa: BLE001
            findings.append(Finding("probe", "fail", f"{str(exc)[:120]}"))
    return findings


def login_preflight(platform: str, sessions: SessionStore, *, check_network: bool = True):
    """Everything that can be known before a window opens.

    Cheap, synchronous, and it runs every time: five minutes staring at a login
    page is a bad way to find out playwright is not installed.
    """
    from .core.login import FLOWS, preflight
    from .scaffold import diagnose

    flow = FLOWS.get(platform)
    if flow is None:
        return [Finding("platform", "fail", f"{platform} has no browser login flow")]

    findings = {f.label: f for f in diagnose()}
    browser = findings.get("chromium")

    reachable = None
    if check_network:
        reachable = _reachable(flow.login_url)

    store_writable = True
    try:
        sessions.dir.mkdir(parents=True, exist_ok=True)
        probe = sessions.dir / ".writable"
        probe.write_text("")
        probe.unlink()
    except Exception:  # noqa: BLE001
        store_writable = False

    return preflight(
        flow,
        have_playwright=bool(findings.get("playwright") and findings["playwright"].ok),
        browser_detail=browser.detail if browser and browser.ok else None,
        existing_session=sessions.load(platform) if sessions.exists(platform) else None,
        store_writable=store_writable,
        reachable=reachable,
    )


def _reachable(url: str, timeout: float = 6.0) -> tuple[bool, str]:
    """Is the login page even up from here?

    A proxy, a VPN or a captive portal produces a blank window and a five-minute
    wait. One HEAD request turns that into one line.
    """
    try:
        import httpx

        r = httpx.head(url, follow_redirects=True, timeout=timeout)
        return True, f"HTTP {r.status_code}"
    except Exception as exc:  # noqa: BLE001
        return False, type(exc).__name__
