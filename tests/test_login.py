# Copyright 2026 The pubkit Authors
# SPDX-License-Identifier: Apache-2.0
"""Every way a sign-in goes wrong, and what pubkit says about each.

The unit half needs nothing but Python: the judgement about what counts as a
valid session is deliberately separate from the browser that produces one, so
the whole matrix is decidable in milliseconds.

The integration half drives a real browser against `fixtures/login_server.py`,
which reproduces the failures observed against live platforms — including the
one that started this: a Continue button that does nothing at all.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from pubkit.core.login import (
    FLOWS,
    Finding,
    LoginFlow,
    Observation,
    is_challenge_script,
    looks_signed_in,
    preflight,
    validate_probe,
    validate_state,
    worst,
)

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

DAY = 86400


def _flow(**over) -> LoginFlow:
    base = {
        "platform": "fake",
        "login_url": "http://localhost/signin",
        "success_url_markers": ("/me/",),
        "required_cookies": ("sid", "uid"),
        "cookie_domain": "localhost",
        "probe_url": "http://localhost/me/",
        "signed_in_markers": ("Drafts",),
    }
    return LoginFlow(**{**base, **over})


def _state(names=("sid", "uid"), *, domain="localhost", days=30):
    exp = time.time() + DAY * days
    return {"cookies": [{"name": n, "domain": domain, "expires": exp} for n in names]}


# ------------------------------------------------------------------ preflight
def test_preflight_refuses_to_open_a_window_that_cannot_work():
    """Every one of these costs the user five minutes if it is not caught here."""
    f = preflight(
        _flow(),
        have_playwright=False,
        browser_detail=None,
        existing_session=None,
        store_writable=False,
        reachable=(False, "DNS failure"),
    )
    assert worst(f) == "fail"
    failed = {x.check for x in f if x.level == "fail"}
    assert failed == {"playwright", "browser", "reachable", "session store"}
    # A failure the user cannot act on is the same as no message at all.
    assert all(x.hint for x in f if x.level == "fail")


def test_preflight_is_quiet_when_everything_is_ready():
    f = preflight(
        _flow(),
        have_playwright=True,
        browser_detail="chromium 1234",
        existing_session=None,
        store_writable=True,
        reachable=(True, "200"),
    )
    assert worst(f) == "ok"


def test_preflight_says_so_rather_than_making_you_sign_in_again():
    f = preflight(
        _flow(),
        have_playwright=True,
        browser_detail="chromium",
        existing_session={"cookies": []},
        store_writable=True,
    )
    existing = next(x for x in f if x.check == "existing session")
    assert existing.level == "warn"
    assert "pubkit auth verify fake" in existing.hint


# ------------------------------------------------------------- state validation
def test_an_empty_session_is_never_saved():
    f = validate_state(_flow(), {"cookies": []})
    assert worst(f) == "fail"
    assert "no cookies" in f[0].detail


def test_stopping_at_the_identity_provider_is_named_as_such():
    """Cookies for accounts.google.com are not cookies for the platform."""
    f = validate_state(_flow(), _state(domain="accounts.google.com"))
    assert worst(f) == "fail"
    assert "no cookie for localhost" in f[0].detail
    assert "identity provider" in f[0].hint


def test_a_missing_required_cookie_fails_by_name():
    f = validate_state(_flow(), _state(names=("sid",)))
    bad = next(x for x in f if x.level == "fail")
    assert bad.check == "required cookies" and "uid" in bad.detail


def test_a_session_that_expires_this_week_is_flagged_now_not_mid_publish():
    f = validate_state(_flow(), _state(days=2))
    life = next(x for x in f if x.check == "session lifetime")
    assert life.level == "warn" and "2." in life.detail
    assert worst(f) == "warn"          # a warning, not a refusal


def test_an_already_expired_session_fails():
    f = validate_state(_flow(), _state(days=-1))
    assert worst(f) == "fail"


def test_cookies_with_no_expiry_warn_because_they_die_with_the_browser():
    st = {"cookies": [{"name": "sid", "domain": "localhost"}, {"name": "uid", "domain": "localhost"}]}
    life = next(x for x in validate_state(_flow(), st) if x.check == "session lifetime")
    assert life.level == "warn" and "restart" in life.detail


def test_a_good_session_passes_cleanly():
    assert worst(validate_state(_flow(), _state())) == "ok"


# ------------------------------------------------------------- probe validation
def test_a_probe_that_bounces_to_sign_in_fails_however_good_the_cookies_look():
    """The ghost session: everything validates, nothing works."""
    f = validate_probe(_flow(), final_url="http://localhost/signin", body_text="Sign in")
    assert f.level == "fail" and "not signed in" in f.detail


def test_a_probe_http_error_fails():
    assert validate_probe(_flow(), final_url="http://localhost/me/", body_text="", status=403).level == "fail"


def test_a_missing_marker_warns_rather_than_fails_because_pages_get_redesigned():
    f = validate_probe(_flow(), final_url="http://localhost/me/", body_text="something else")
    assert f.level == "warn" and "redesigned" in f.hint


def test_a_real_signed_in_page_passes():
    assert validate_probe(_flow(), final_url="http://localhost/me/", body_text="Drafts").level == "ok"


# ------------------------------------------------------------- success signals
def test_either_a_url_marker_or_the_cookies_is_enough():
    f = _flow()
    assert looks_signed_in(f, "http://localhost/me/", set())
    assert looks_signed_in(f, "http://localhost/anywhere", {"sid", "uid"})
    assert not looks_signed_in(f, "http://localhost/signin", {"sid"})


def test_challenge_scripts_are_recognised_by_host():
    assert is_challenge_script("https://www.google.com/recaptcha/api.js")
    assert is_challenge_script("https://client-api.arkoselabs.com/v2/x.js")
    assert is_challenge_script("https://challenges.cloudflare.com/turnstile/v0/api.js")
    assert not is_challenge_script("https://medium.com/static/bundle.js")


# --------------------------------------------------------------- the diagnosis
def test_a_button_that_never_fires_is_reported_as_exactly_that():
    """The failure that started all this: click, and nothing happens."""
    obs = Observation()
    obs.note_url("http://localhost/signin")
    f = obs.diagnose(_flow())
    sub = next(x for x in f if x.check == "form submission")
    assert sub.level == "fail"
    assert "never sent a sign-in request" in sub.detail
    assert "--attach" in sub.hint


def test_a_blocked_bot_check_is_distinguished_from_a_dead_button():
    obs = Observation()
    obs.note_url("http://localhost/signin")
    obs.challenge_scripts.append("https://www.google.com/recaptcha/api.js")
    obs.failed_requests.append("GET https://www.google.com/recaptcha/api.js :: net::ERR_FAILED")
    checks = {x.check: x for x in obs.diagnose(_flow())}
    assert checks["bot check"].level == "warn"
    assert "google.com" in checks["bot check"].detail
    assert checks["network"].level == "fail"


def test_a_quiet_page_says_what_it_was_waiting_for():
    obs = Observation()
    obs.note_url("http://localhost/signin")
    obs.submits = 1
    page = next(x for x in obs.diagnose(_flow()) if x.check == "page")
    assert "/me/" in page.hint and "sid" in page.hint


def test_every_shipped_flow_is_complete():
    """A new platform cannot be half-specified — the checks all key off this."""
    for name, flow in FLOWS.items():
        assert flow.platform == name
        assert flow.login_url.startswith("https://")
        assert flow.probe_url.startswith("https://")
        assert flow.required_cookies, f"{name} has no cookie that proves a session"
        assert flow.cookie_domain in flow.probe_url
        assert flow.success_url_markers


def test_findings_render_with_their_hint():
    s = str(Finding("probe", "fail", "nope", "do this"))
    assert "✗" in s and "nope" in s and "do this" in s


# ============================================================== integration
pytest.importorskip("playwright.async_api", reason="playwright not installed")


@pytest.fixture
def server():
    from login_server import LoginServer

    with LoginServer() as s:
        yield s


@pytest.fixture
def store(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    from pubkit.core.auth import SessionStore

    monkeypatch.setenv("PUBKIT_PROFILE_DIR", str(tmp_path / "profiles"))
    # A throwaway vault key, so the test never reaches for the OS keychain —
    # which does not exist on a CI runner and is not the thing under test.
    monkeypatch.setenv("PUBKIT_VAULT_KEY", Fernet.generate_key().decode())
    return SessionStore(dir=tmp_path / "sessions")


def _register(server, mode: str) -> str:
    """Add a throwaway flow pointed at one mode of the fake server."""
    name = f"fake-{mode}"
    FLOWS[name] = LoginFlow(
        platform=name,
        login_url=f"{server.base}/{mode}/signin",
        success_url_markers=(f"/{mode}/me/",),
        required_cookies=("sid", "uid"),
        cookie_domain="localhost",
        probe_url=f"{server.base}/{mode}/me/",
        signed_in_markers=("Drafts",),
    )
    return name


async def _login(name, store, **kw):
    from pubkit.browserctl import interactive_login

    try:
        return await interactive_login(
            name, store, timeout=kw.pop("timeout", 12), headless=True, report=lambda *_: None, **kw
        )
    except Exception as exc:  # pragma: no cover
        msg = str(exc)
        if "executable doesn" in msg or "Executable doesn" in msg or "browserType.launch" in msg:
            pytest.skip(f"browser unavailable: {msg[:90]}")
        raise


async def _click_continue(name, delay=1.5):
    """Stand in for the human, once the window is up."""
    import asyncio

    await asyncio.sleep(delay)
    # The page is driven by the real browser the login opened; we reach it the
    # same way a person would — by clicking the button.


async def test_a_completed_sign_in_is_validated_then_saved(server, store, monkeypatch):
    """The happy path, end to end: click, cookies, probe, save."""
    import asyncio

    name = _register(server, "ok")
    from pubkit import browserctl

    real = browserctl._open_login_context

    async def wrapper(pw, platform, *, attach, headless=False):
        ctx, close, how = await real(pw, platform, attach=attach, headless=headless)

        async def human():
            await asyncio.sleep(2)
            with_page = ctx.pages[0]
            await with_page.click("#continue")

        asyncio.ensure_future(human())
        return ctx, close, how

    monkeypatch.setattr(browserctl, "_open_login_context", wrapper)
    findings = await _login(name, store)

    checks = {f.check: f for f in findings}
    assert worst(findings) != "fail", [str(f) for f in findings]
    assert checks["required cookies"].level == "ok"
    assert checks["probe"].level == "ok"
    assert checks["saved"].level == "ok"
    assert store.load(name) is not None


async def test_a_dead_button_times_out_with_a_diagnosis_not_a_shrug(server, store):
    """No click handler at all. pubkit must say so, and save nothing."""
    name = _register(server, "dead")
    findings = await _login(name, store, timeout=8)

    checks = {f.check: f for f in findings}
    assert checks["timeout"].level == "fail"
    assert checks["form submission"].level == "fail"
    assert "--attach" in checks["form submission"].hint
    assert store.load(name) is None


async def test_a_blocked_bot_check_names_the_script(server, store, monkeypatch):
    """Identical symptom to the dead button; different, nameable cause."""
    import asyncio

    name = _register(server, "challenge")
    from pubkit import browserctl

    real = browserctl._open_login_context

    async def wrapper(pw, platform, *, attach, headless=False):
        ctx, close, how = await real(pw, platform, attach=attach, headless=headless)

        async def human():
            await asyncio.sleep(2)
            await ctx.pages[0].click("#continue")

        asyncio.ensure_future(human())
        return ctx, close, how

    monkeypatch.setattr(browserctl, "_open_login_context", wrapper)
    findings = await _login(name, store, timeout=9)

    checks = {f.check: f for f in findings}
    assert checks["timeout"].level == "fail"
    # The click WAS handled, so this is not the dead-button case.
    assert checks["bot check"].level == "warn"
    assert store.load(name) is None


async def test_a_session_missing_a_cookie_is_not_saved(server, store, monkeypatch):
    import asyncio

    name = _register(server, "partial")
    from pubkit import browserctl

    real = browserctl._open_login_context

    async def wrapper(pw, platform, *, attach, headless=False):
        ctx, close, how = await real(pw, platform, attach=attach, headless=headless)

        async def human():
            await asyncio.sleep(2)
            await ctx.pages[0].click("#continue")

        asyncio.ensure_future(human())
        return ctx, close, how

    monkeypatch.setattr(browserctl, "_open_login_context", wrapper)
    findings = await _login(name, store, timeout=12)

    checks = {f.check: f for f in findings}
    assert checks["required cookies"].level == "fail"
    assert checks["saved"].level == "fail"
    assert store.load(name) is None


async def test_a_ghost_session_validates_and_is_still_rejected(server, store, monkeypatch):
    """Both cookies present, probe still bounces. Only the probe catches this."""
    import asyncio

    name = _register(server, "ghost")
    from pubkit import browserctl

    real = browserctl._open_login_context

    async def wrapper(pw, platform, *, attach, headless=False):
        ctx, close, how = await real(pw, platform, attach=attach, headless=headless)

        async def human():
            await asyncio.sleep(2)
            await ctx.pages[0].click("#continue")

        asyncio.ensure_future(human())
        return ctx, close, how

    monkeypatch.setattr(browserctl, "_open_login_context", wrapper)
    findings = await _login(name, store, timeout=12)

    checks = {f.check: f for f in findings}
    assert checks["required cookies"].level == "ok"      # cookies are fine
    assert checks["probe"].level == "fail"               # the platform disagrees
    assert store.load(name) is None
