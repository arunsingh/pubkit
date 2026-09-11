# Copyright 2026 The pubkit Authors
# SPDX-License-Identifier: Apache-2.0
"""What has to be true before, during and after a sign-in.

A login is the one step in pubkit that a human performs and a machine has to
judge. That asymmetry is where the failures live, and they are all quiet:

  * the window opens and the submit button does nothing at all — no error, no
    request, nothing to read
  * the window opens against the wrong URL and waits five minutes for a marker
    that can never appear
  * sign-in succeeds, a session is saved, and it turns out to hold no cookie
    for the platform's own domain
  * a session is saved that expires tonight, and the failure surfaces in the
    middle of a publish a week later
  * a session is saved that looks complete and simply is not signed in

None of those announce themselves. So this module states, per platform, what a
completed login must look like, and checks it in three phases:

  preflight   — refuse to open a window that cannot possibly work
  observe     — record what the page did, so a dead button leaves evidence
  postflight  — a saved session is one that has been proven to work

Nothing here drives a browser. `browserctl` does that; this module decides what
counts as correct, which is why it can be tested without one.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal

Level = Literal["ok", "warn", "fail"]


@dataclass(frozen=True)
class Finding:
    """One checked thing, and what to do if it is wrong.

    `hint` is not decoration. A login failure the user cannot act on is the
    same as no message at all.
    """

    check: str
    level: Level
    detail: str
    hint: str = ""

    @property
    def ok(self) -> bool:
        return self.level != "fail"

    def __str__(self) -> str:
        mark = {"ok": "✓", "warn": "!", "fail": "✗"}[self.level]
        line = f"  {mark} {self.check}: {self.detail}"
        return f"{line}\n      {self.hint}" if self.hint else line


@dataclass(frozen=True)
class LoginFlow:
    """Everything platform-specific about signing in, in one place.

    Adding a platform means adding one of these. Every check in this module,
    and every test, is driven off it — so a new platform inherits the whole
    validation suite rather than a new pile of special cases.
    """

    platform: str
    login_url: str
    #: Any one of these appearing in the URL means the human is through.
    success_url_markers: tuple[str, ...]
    #: Cookies without which a session is not a session. Presence of all of
    #: these is an independent success signal, because some platforms land the
    #: user back on a URL indistinguishable from the signed-out one.
    required_cookies: tuple[str, ...]
    #: Cookies must belong to this registrable domain, not to an SSO hop.
    cookie_domain: str
    #: A page that renders only for a signed-in user.
    probe_url: str
    #: Seen on the probe when signed in.
    signed_in_markers: tuple[str, ...] = ()
    #: Seen in the probe's URL when the session is dead.
    signed_out_url_markers: tuple[str, ...] = ("signin", "sign-in", "login")
    #: Warn when the session dies sooner than this.
    min_session_days: int = 7


FLOWS: dict[str, LoginFlow] = {
    "medium": LoginFlow(
        platform="medium",
        login_url="https://medium.com/m/signin",
        # Medium bounces through several URLs after sign-in and none of them is
        # reliably the same twice, so cookie presence carries most of the load.
        success_url_markers=("medium.com/me/", "medium.com/?", "medium.com/new-story"),
        required_cookies=("sid", "uid"),
        cookie_domain="medium.com",
        probe_url="https://medium.com/me/stories/drafts",
        signed_in_markers=("Drafts", "New story"),
    ),
    "substack": LoginFlow(
        platform="substack",
        login_url="https://substack.com/sign-in",
        success_url_markers=("substack.com/home", "substack.com/inbox"),
        required_cookies=("substack.sid",),
        cookie_domain="substack.com",
        probe_url="https://substack.com/home",
        signed_in_markers=("Inbox",),
    ),
}


# --------------------------------------------------------------------- phase 1
def preflight(
    flow: LoginFlow,
    *,
    have_playwright: bool,
    browser_detail: str | None,
    existing_session: dict | None,
    store_writable: bool,
    reachable: tuple[bool, str] | None = None,
) -> list[Finding]:
    """Refuse to open a window that cannot work.

    Every argument is a fact the caller has already established. Keeping the
    I/O outside means this is decidable in a unit test, which is the only
    reason the matrix below is actually covered.
    """
    out: list[Finding] = []

    out.append(
        Finding("playwright", "ok", "installed")
        if have_playwright
        else Finding(
            "playwright",
            "fail",
            "not installed",
            'pip install "pubkit[browser]"',
        )
    )

    out.append(
        Finding("browser", "ok", browser_detail)
        if browser_detail
        else Finding(
            "browser",
            "fail",
            "no Chromium or Chrome found",
            "python -m playwright install chromium",
        )
    )

    if reachable is not None:
        up, detail = reachable
        out.append(
            Finding("reachable", "ok", detail)
            if up
            else Finding(
                "reachable",
                "fail",
                f"cannot reach {flow.login_url}: {detail}",
                "check your network, VPN or proxy before blaming the login",
            )
        )

    out.append(
        Finding("session store", "ok", "writable")
        if store_writable
        else Finding(
            "session store",
            "fail",
            "not writable",
            "pubkit cannot save what you are about to do — fix permissions first",
        )
    )

    if existing_session is not None:
        out.append(
            Finding(
                "existing session",
                "warn",
                f"a {flow.platform} session is already saved",
                f"pubkit auth verify {flow.platform} — signing in again is only "
                "needed if that reports it dead",
            )
        )

    return out


# --------------------------------------------------------------------- phase 2
@dataclass
class Observation:
    """What the page did while the human was working.

    A submit button that does nothing is the hardest login failure to report,
    because from the outside nothing happened. The difference between "nothing
    happened" and "a request to a bot-check endpoint failed" is the difference
    between a shrug and a fix, so both are recorded as they occur.
    """

    console_errors: list[str] = field(default_factory=list)
    failed_requests: list[str] = field(default_factory=list)
    challenge_scripts: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    submits: int = 0

    def note_url(self, url: str) -> None:
        if not self.urls or self.urls[-1] != url:
            self.urls.append(url)

    def diagnose(self, flow: LoginFlow) -> list[Finding]:
        """Turn a silent five-minute timeout into something actionable."""
        out: list[Finding] = []
        last = self.urls[-1] if self.urls else "(never loaded)"
        out.append(Finding("last URL", "warn", last))

        if self.submits == 0:
            out.append(
                Finding(
                    "form submission",
                    "fail",
                    "the page never sent a sign-in request",
                    "the button was not wired up, or a script it waits on never "
                    f"finished — try: pubkit auth login {flow.platform} --attach",
                )
            )
        else:
            out.append(Finding("form submission", "ok", f"{self.submits} request(s) sent"))

        if self.challenge_scripts:
            out.append(
                Finding(
                    "bot check",
                    "warn",
                    f"page loads {len(self.challenge_scripts)} challenge script(s): "
                    + ", ".join(sorted({_host(s) for s in self.challenge_scripts})),
                    "these frequently refuse to complete in a freshly launched "
                    f"profile — pubkit auth login {flow.platform} --attach uses "
                    "the browser you already sign in with",
                )
            )

        if self.failed_requests:
            out.append(
                Finding(
                    "network",
                    "fail" if self.submits == 0 else "warn",
                    f"{len(self.failed_requests)} request(s) failed",
                    "first: " + self.failed_requests[0],
                )
            )

        if self.console_errors:
            out.append(
                Finding(
                    "console",
                    "warn",
                    f"{len(self.console_errors)} error(s)",
                    "first: " + self.console_errors[0][:160],
                )
            )

        if not (self.failed_requests or self.console_errors or self.challenge_scripts):
            out.append(
                Finding(
                    "page",
                    "warn",
                    "no errors reported — the sign-in was most likely just not finished",
                    f"pubkit watches for {', '.join(flow.success_url_markers)} "
                    f"or the cookies {', '.join(flow.required_cookies)}",
                )
            )
        return out


CHALLENGE_HOSTS = (
    "recaptcha",
    "hcaptcha",
    "captcha",
    "arkoselabs",
    "funcaptcha",
    "perimeterx",
    "datadome",
    "challenges.cloudflare.com",
)


def is_challenge_script(url: str) -> bool:
    low = url.lower()
    return any(h in low for h in CHALLENGE_HOSTS)


def _host(url: str) -> str:
    return url.split("//", 1)[-1].split("/", 1)[0]


def looks_signed_in(flow: LoginFlow, url: str, cookie_names: set[str]) -> bool:
    """Two independent signals, because either one alone misses cases.

    A URL marker misses platforms that land you somewhere generic. Cookies miss
    platforms that set them before the flow is actually finished. Requiring
    either keeps the window from hanging on a login that plainly succeeded.
    """
    if any(m in url for m in flow.success_url_markers):
        return True
    return bool(flow.required_cookies) and set(flow.required_cookies) <= cookie_names


# --------------------------------------------------------------------- phase 3
def validate_state(flow: LoginFlow, storage_state: dict, *, now: float | None = None) -> list[Finding]:
    """A session is only worth saving if it can be shown to be one.

    This is the check that would have caught every "it said it worked and then
    the publish failed" report before it was filed.
    """
    now = time.time() if now is None else now
    out: list[Finding] = []
    cookies = storage_state.get("cookies") or []

    if not cookies:
        return [
            Finding(
                "cookies",
                "fail",
                "the saved session contains no cookies at all",
                "the sign-in did not complete — nothing has been saved",
            )
        ]

    ours = [c for c in cookies if flow.cookie_domain in (c.get("domain") or "")]
    if not ours:
        domains = sorted({(c.get("domain") or "?").lstrip(".") for c in cookies})[:4]
        return [
            Finding(
                "cookie domain",
                "fail",
                f"no cookie for {flow.cookie_domain}; found {', '.join(domains)}",
                "the flow stopped at an identity provider and never came back",
            )
        ]
    out.append(Finding("cookie domain", "ok", f"{len(ours)} cookie(s) for {flow.cookie_domain}"))

    names = {c.get("name") for c in ours}
    missing = [c for c in flow.required_cookies if c not in names]
    if missing:
        out.append(
            Finding(
                "required cookies",
                "fail",
                f"missing {', '.join(missing)}",
                "signed in as far as the browser is concerned, but not in a way "
                "that authorises writing — sign in fully, not just to the profile",
            )
        )
    else:
        out.append(
            Finding("required cookies", "ok", ", ".join(flow.required_cookies) or "none required")
        )

    # A session cookie with no expiry dies with the browser; a short one dies
    # mid-week. Both are worth saying out loud now rather than discovering later.
    expiries = [c["expires"] for c in ours if isinstance(c.get("expires"), (int, float)) and c["expires"] > 0]
    if not expiries:
        out.append(
            Finding(
                "session lifetime",
                "warn",
                "no persistent expiry — this may not survive a restart",
                "re-run login if a later publish reports an expired session",
            )
        )
    else:
        days = (max(expiries) - now) / 86400
        if days <= 0:
            out.append(
                Finding("session lifetime", "fail", "already expired", "sign in again")
            )
        elif days < flow.min_session_days:
            out.append(
                Finding(
                    "session lifetime",
                    "warn",
                    f"expires in {days:.1f} day(s)",
                    "short-lived; expect to sign in again soon",
                )
            )
        else:
            out.append(Finding("session lifetime", "ok", f"{days:.0f} day(s)"))

    return out


def validate_probe(
    flow: LoginFlow, *, final_url: str, body_text: str, status: int | None = None
) -> Finding:
    """The only check that actually proves anything: fetch a signed-in page.

    Everything before this is inference about cookies. This asks the platform.
    """
    if status is not None and status >= 400:
        return Finding("probe", "fail", f"{flow.probe_url} returned HTTP {status}", "sign in again")

    if any(m in final_url for m in flow.signed_out_url_markers):
        return Finding(
            "probe",
            "fail",
            f"redirected to {final_url} — not signed in",
            f"pubkit auth login {flow.platform}",
        )

    if flow.signed_in_markers and not any(m in body_text for m in flow.signed_in_markers):
        return Finding(
            "probe",
            "warn",
            "loaded, but none of "
            + ", ".join(repr(m) for m in flow.signed_in_markers)
            + " is on the page",
            "the page may have been redesigned — check it by hand before publishing",
        )

    return Finding("probe", "ok", f"{flow.probe_url} loads as a signed-in user")


def worst(findings: list[Finding]) -> Level:
    if any(f.level == "fail" for f in findings):
        return "fail"
    if any(f.level == "warn" for f in findings):
        return "warn"
    return "ok"
