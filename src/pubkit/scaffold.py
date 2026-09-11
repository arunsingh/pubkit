# Copyright 2026 The pubkit Authors
# SPDX-License-Identifier: Apache-2.0
"""`pubkit init` and `pubkit doctor`.

Adoption is mostly a first-five-minutes problem. `init` gives someone a content
repo that already validates and already has a working CI pipeline; `doctor`
answers "why isn't this working" without them having to read the source.
"""
from __future__ import annotations

import importlib.util
import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path

SAMPLE_POST = '''---
id: hello-world
title: "The thing I learned the hard way"
subtitle: A one-line promise of what the reader gets.
tags: [Engineering, Writing]
canonical_url: ""
# Uncomment to have the build check your length:
# budget: {words: 1200, tolerance: 0.15}
---

# What this is about

Open with the specific, surprising fact. Not a preamble — the fact.

Numbers you will reuse can be declared once and checked by the build:

<!-- pubkit:define fast = 900 -->
<!-- pubkit:define slow = 128 -->

The fast path is 7× wider than the slow one. <!-- pubkit:assert 7x = fast/slow ±10% -->

## A section with evidence

| Approach | Throughput |
| :--- | ---: |
| Naive | 128 MB/s |
| Tuned | 900 MB/s |
*Measured on the same hardware, same dataset*

> [!key] The rule
> State the rule you want the reader to remember, once, in its own block.

```python
def example() -> None:
    print("code blocks survive to every platform that supports them")
```

Close with what changes for the reader tomorrow.
'''

SAMPLE_SERIES_NOTE = '''---
id: part-1
title: "My Series, Part 1: The Setup"
subtitle: What part one establishes.
tags: [Engineering]
series: {id: my-series, index: 1, of: 2}
---

# The first idea

Cross-links resolve themselves during a two-phase publish:

Continued in [Part 2](${series.part2.url}).
'''

CONFIG = """# pubkit.toml — optional. Everything here can also be a CLI flag.
[defaults]
platforms = ["medium", "devto"]

[medium]
# Medium has no tables; pubkit renders yours as styled images automatically.

[devto]
# Dev.to wants images at public URLs. Point this at wherever you host them.
# asset_base_url = "https://raw.githubusercontent.com/you/blog/main/"

[x]
mode = "promo"        # "promo" (hook + claims + link) or "full"
max_posts = 12
"""

CI = """name: publish
on:
  pull_request:
    paths: ["content/**"]
  push:
    branches: [main]
    paths: ["content/**"]

jobs:
  plan:
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: "3.12"}
      - run: pip install "pubkit[all]"
      - run: pubkit validate content/ --strict
      - name: Plan
        run: |
          echo '## Publishing plan' >> $GITHUB_STEP_SUMMARY
          echo '```' >> $GITHUB_STEP_SUMMARY
          pubkit plan content/ --to medium,devto >> $GITHUB_STEP_SUMMARY
          echo '```' >> $GITHUB_STEP_SUMMARY

  publish:
    if: github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    environment: production   # add a required reviewer here for a human gate
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: "3.12"}
      - run: pip install "pubkit[all]"
      - run: pubkit publish content/ --to devto --confirm
        env:
          PUBKIT_DEVTO_TOKEN: ${{ secrets.DEVTO_TOKEN }}
"""

README = """# Content

Written once here, published everywhere by [pubkit](https://github.com/arunsingh/pubkit).

```bash
pubkit validate content/            # check before anything leaves your laptop
pubkit plan content/ --to medium    # see exactly what the platform will get
pubkit publish content/ --to medium # draft
pubkit publish content/ --to medium --confirm   # live
```

`.pubkit/` holds the state store and your encrypted sessions. It is gitignored,
and it is what makes a re-run resume instead of duplicating your drafts.
"""


def init_repo(root: Path, *, series: bool = False, force: bool = False) -> list[Path]:
    """Scaffold a content repository that validates on the first try."""
    written: list[Path] = []

    def write(rel: str, text: str) -> None:
        p = root / rel
        if p.exists() and not force:
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        written.append(p)

    write("content/hello-world.md", SAMPLE_POST)
    if series:
        write("content/series/part-1.md", SAMPLE_SERIES_NOTE)
        write(
            "content/series/part-2.md",
            SAMPLE_SERIES_NOTE.replace("part-1", "part-2")
            .replace("index: 1", "index: 2")
            .replace("Part 1: The Setup", "Part 2: The Payoff")
            .replace("What part one establishes.", "What part two delivers.")
            .replace("Continued in [Part 2](${series.part2.url}).", "Back to [Part 1](${series.part1.url})."),
        )
    write("content/img/.gitkeep", "")
    write("pubkit.toml", CONFIG)
    write(".github/workflows/publish.yml", CI)
    write("README.md", README)
    write(".gitignore", ".pubkit/\n__pycache__/\n")
    return written


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------
@dataclass
class Finding:
    ok: bool
    label: str
    detail: str
    fix: str = ""


def _has(mod: str) -> bool:
    return importlib.util.find_spec(mod) is not None


def diagnose() -> list[Finding]:
    """Answer 'why isn't this working' before anyone has to read the source."""
    out: list[Finding] = []

    v = sys.version_info
    out.append(
        Finding(
            v >= (3, 11),
            "python",
            f"{v.major}.{v.minor}.{v.micro} on {platform.system()}",
            "pubkit needs Python 3.11+",
        )
    )

    out.append(
        Finding(
            _has("playwright"),
            "playwright",
            "installed" if _has("playwright") else "missing",
            'pip install "pubkit[browser]" && playwright install chromium '
            "— required for Medium and Substack only",
        )
    )

    if _has("playwright"):
        # Honour PLAYWRIGHT_BROWSERS_PATH. Managed images (CI runners, devcontainers,
        # the Playwright Docker image) put browsers somewhere else entirely, and
        # reporting "not downloaded" when they are right there sends people off to
        # fix a problem they do not have.
        override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
        if override and override != "0":
            roots = [Path(override)]
            where = override
        else:
            roots = [
                Path.home()
                / (
                    "Library/Caches/ms-playwright"
                    if platform.system() == "Darwin"
                    else "AppData/Local/ms-playwright"
                    if platform.system() == "Windows"
                    else ".cache/ms-playwright"
                )
            ]
            where = "the default cache"
        found = [d for r in roots if r.exists() for d in r.glob("chromium*")]
        out.append(
            Finding(
                bool(found),
                "chromium",
                f"{len(found)} build(s) in {where}" if found else f"none in {where}",
                "playwright install chromium",
            )
        )

    out.append(
        Finding(
            _has("keyring"),
            "keyring",
            "available — tokens go to the OS keychain" if _has("keyring") else "missing",
            'pip install "pubkit[keychain]", or set PUBKIT_VAULT_KEY for an encrypted file vault',
        )
    )
    out.append(
        Finding(
            _has("cryptography"),
            "cryptography",
            "available — sessions encrypted at rest" if _has("cryptography") else "missing",
            'pip install "pubkit[keychain]" — without it, saved sessions are stored in plain text',
        )
    )

    state = Path(".pubkit/state.sqlite")
    out.append(
        Finding(
            True,
            "state store",
            f"{state} ({state.stat().st_size} bytes)" if state.exists() else "not created yet (normal)",
        )
    )

    from .core.auth import SessionStore, TokenStore
    from .registry import list_adapters

    tokens, sessions = TokenStore(), SessionStore()
    for name, _ in list_adapters():
        has_tok = bool(tokens.get(name)) or bool(tokens.get(name, "bearer"))
        has_sess = sessions.exists(name)
        out.append(
            Finding(
                has_tok or has_sess,
                f"auth:{name}",
                "token" if has_tok else "session" if has_sess else "not configured",
                f"pubkit auth login {name}",
            )
        )

    env = [k for k in os.environ if k.startswith("PUBKIT_")]
    if env:
        out.append(Finding(True, "env", ", ".join(sorted(env))))

    return out
