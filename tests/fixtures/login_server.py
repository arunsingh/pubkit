# Copyright 2026 The pubkit Authors
# SPDX-License-Identifier: Apache-2.0
"""A sign-in page that fails the way real ones fail.

Every mode here is a failure that was observed against a live platform, not an
invented one. They are all the same from the outside — a page, a button, and
nothing happening — which is exactly why they need to be separated in a test.

    ok         sign-in completes and sets both session cookies
    dead       the button has no handler at all: clicking does nothing, no
               request is made, no error is logged. The single most confusing
               login failure there is.
    challenge  the page waits on a bot-check script that never loads, so the
               handler never reaches the submit. Looks identical to `dead`
               from the user's chair; completely different cause.
    partial    signs in and sets only one of the two required cookies
    short      signs in with cookies that expire tomorrow
    ghost      sets both cookies, but the signed-in page still bounces you
               back to sign-in — a session that validates and does not work

Run it on localhost; `cookie_domain` for the matching LoginFlow is "localhost".
"""
from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODES = ("ok", "dead", "challenge", "partial", "short", "ghost")

_PAGE = """<!doctype html><meta charset=utf-8><title>Sign in</title>
{extra_head}
<h1>Sign in with email</h1>
<form id="f" onsubmit="return false">
  <input id="email" value="you@example.test">
  <button id="continue" type="button">Continue</button>
</form>
<script>
const MODE = "{mode}";
const btn = document.getElementById('continue');

if (MODE === "dead") {{
  // No handler is attached. This is the bug: a button that is not wired up is
  // indistinguishable from one that is blocked, and neither says anything.
}} else if (MODE === "challenge") {{
  btn.addEventListener('click', async () => {{
    // Waits for a bot-check global that the (404ing) script never defines.
    // The click IS handled — it simply never gets to the submit.
    await new Promise(res => {{
      const t = setInterval(() => {{
        if (window.grecaptcha) {{ clearInterval(t); res(); }}
      }}, 100);
    }});
    fetch("/{mode}/submit", {{method: "POST"}});
  }});
}} else {{
  btn.addEventListener('click', async () => {{
    await fetch("/{mode}/submit", {{method: "POST"}});
    location.href = "/{mode}/me/";
  }});
}}
</script>
"""

_DRAFTS = """<!doctype html><meta charset=utf-8><title>Drafts</title>
<h1>Drafts</h1><p>New story</p><p>Signed in.</p>
"""


def _cookies_for(mode: str) -> list[str]:
    far = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() + 86400 * 30))
    soon = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() + 86400))
    if mode == "partial":
        return [f"sid=abc; Path=/; Expires={far}"]
    if mode == "short":
        return [f"sid=abc; Path=/; Expires={soon}", f"uid=42; Path=/; Expires={soon}"]
    return [f"sid=abc; Path=/; Expires={far}", f"uid=42; Path=/; Expires={far}"]


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # keep pytest output readable
        pass

    def _split(self):
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        mode = parts[0] if parts and parts[0] in MODES else "ok"
        rest = "/" + "/".join(parts[1:])
        return mode, rest

    def do_GET(self):  # noqa: N802
        mode, rest = self._split()

        if rest.startswith("/recaptcha"):
            self.send_error(404)
            return

        if rest in ("/signin", "/"):
            extra = ""
            if mode == "challenge":
                # A real bot-check script tag, pointing at a URL that 404s.
                extra = f'<script src="/{mode}/recaptcha/api.js"></script>'
            body = _PAGE.format(mode=mode, extra_head=extra).encode()
            self._respond(200, body)
            return

        if rest.startswith("/me"):
            has = "sid=" in (self.headers.get("Cookie") or "")
            if not has or mode == "ghost":
                self.send_response(302)
                self.send_header("Location", f"/{mode}/signin")
                self.end_headers()
                return
            self._respond(200, _DRAFTS.encode())
            return

        self.send_error(404)

    def do_POST(self):  # noqa: N802
        mode, rest = self._split()
        if rest != "/submit":
            self.send_error(404)
            return
        self.send_response(200)
        for c in _cookies_for(mode):
            self.send_header("Set-Cookie", c)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def _respond(self, code: int, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class LoginServer:
    """Context manager yielding a base URL such as http://localhost:54321."""

    def __init__(self) -> None:
        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self._srv.server_address[1]
        self.base = f"http://localhost:{self.port}"

    def __enter__(self) -> LoginServer:
        self._t = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._t.start()
        return self

    def __exit__(self, *exc) -> None:
        self._srv.shutdown()
        self._srv.server_close()
