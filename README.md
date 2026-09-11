# pubkit

**Publish one source to many platforms, safely.**
Medium · Substack · X · Dev.to · Hashnode — and whatever you add next.

[![PyPI](https://img.shields.io/pypi/v/pubkit.svg)](https://pypi.org/project/pubkit/)
[![Python](https://img.shields.io/pypi/pyversions/pubkit.svg)](https://pypi.org/project/pubkit/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![CI](https://github.com/arunsingh/pubkit/actions/workflows/ci.yml/badge.svg)](https://github.com/arunsingh/pubkit/actions/workflows/ci.yml)

---

## Why this exists

I published a 15,000-word, three-part illustrated series to Medium. It took far
longer than writing it, and not for any reason I could have predicted.

A 12,368-character payload arrived with exactly **one byte** changed, and
nothing reported an error. Pasting a document replaced 2 of 169 paragraphs and
silently appended the rest, producing a scrambled duplicate. A delete that
visibly worked was back after a reload. Link hrefs set in the DOM reverted every
time. Every image — `data:` URI and `https://` URL alike — was stripped out of
pasted HTML, and it took eight dead ends to find the one insertion path that
works. A scripted check of 32 numeric claims found 5 that were wrong, including
two ratios transposed in an edit that no amount of re-reading had caught. And
the automation bridge dropped eight times, so everything had to be re-runnable.

None of that is Medium being unusual. It is what automating *any* rich-text
editor looks like. pubkit is that knowledge, extracted and made reusable.

[`docs/FAILURE-MODES.md`](docs/FAILURE-MODES.md) catalogues all of it — every
entry names the component that answers it.

## Install

```bash
pip install "pubkit[all]"
playwright install chromium     # Medium and Substack only
pubkit doctor                   # confirms your environment before you start
```

Thirty seconds to something real:

```bash
pubkit init my-blog && cd my-blog
pubkit validate content/
pubkit plan content/ --to medium,devto
```

## Use it

```bash
# 0. Scaffold a content repo that validates on the first try.
pubkit init my-blog && cd my-blog

# 1. Check the content. No network, no browser, no side effects.
pubkit validate content/series/

# 2. See exactly what each platform will get — including every degradation.
pubkit plan content/series/ --to medium,devto,x

# 3. Sign in, once. pubkit never accepts a password.
pubkit auth login medium        # a window opens; you sign in; it keeps the session
pubkit auth login devto         # API token → OS keychain
pubkit auth verify medium       # confirm before a publish depends on it

# 4. Drafts everywhere. Safe to re-run; a dropped connection costs one step.
pubkit publish content/series/ --to medium,devto,x

# 5. Go live.
pubkit publish content/series/ --to medium,devto,x --confirm
```

`plan` output on a real series:

```
medium: part-1 (bf9abfe78ce4)
  · images_via_browser   4 image(s) uploaded through the editor's own paste path
                         — remote URLs and data: URIs are stripped by this platform
  · code_flattened       4 code block(s) lose syntax highlighting

devto: part-1 (bf9abfe78ce4)
  · tags_trimmed         keeping 4 of 5

x: part-1 (bf9abfe78ce4)
  · split_into_thread    promo thread: 12 posts — hook, key claims, link to the full article
  · tags_trimmed         5 tag(s) dropped — platform has no tags
```

Nothing is a surprise at publish time. That is the whole design goal.

## Sign in once, then it runs on its own

```bash
pubkit auth login medium
```

A real browser window opens at Medium's login page. You sign in — password
manager, MFA, device confirmation, whatever it asks for. pubkit watches for the
post-login URL, saves the session encrypted, and closes the window.

From then on, unattended:

```bash
pubkit publish content/ --to medium,devto,x --confirm
```

One browser serves the whole run, one context per platform so a Medium session
is never presented to Substack, and sessions refresh on the way out because
cookies rotate. Publishing only to API platforms never launches Chromium at all.

pubkit sees cookies. It never sees, types or stores a password — which is both
the right security posture and the only thing that works, since platforms
increasingly gate login behind challenges an automation layer has no business
trying to defeat.

## Write once

````markdown
---
id: part-1
title: "Inside AI Infrastructure, Part 1: The Hardware"
subtitle: Why a GPU is fast, and why it turns out to be a memory problem.
tags: [AI Infrastructure, GPU, SRE]
series: {id: inside-ai, index: 1, of: 3}
budget: {words: 3500}
---

# What is behind the box you type into

<!-- pubkit:define nvlink = 900 -->
<!-- pubkit:define pcie5 = 128 -->

NVLink is 7× wider than PCIe Gen5. <!-- pubkit:assert 7x = nvlink/pcie5 ±10% -->

| Model | FP16 size |
| :--- | ---: |
| 70B  | 140 GB |
*What the weights actually cost*

![memory layout](img/vram.gif)
*Every token costs one full read of the model out of VRAM.*

Continued in [Part 2](${series.part2.url}).
````

That one file becomes: a Medium draft with the table rendered as a styled image
and the GIF uploaded through the editor's own path; a Dev.to article with a
native table and a fenced code block; a 12-post promo thread on X with a link
back. The cross-link resolves itself during a two-phase publish.

## What it actually guards against

| Guard | What it prevents |
|---|---|
| Hash-verified chunked transport | a silently corrupted payload |
| Probed chunk ceiling | undocumented per-call size limits |
| `replace_document()` with a block-count assert | a paste that appends instead of replacing |
| Reload-then-fingerprint verification | edits the editor showed you but never saved |
| Caption-anchored `File` paste | images stripped out of pasted HTML |
| Unicode-folding anchor resolution | an em dash breaking a text match |
| Tag-chip post-condition | one 40-character invalid tag instead of five good ones |
| `pubkit:assert` arithmetic | a transposed ratio reaching print |
| Two-phase series publish | `URL-PART-2` going live as a link |
| Content-addressed state store | a re-run creating a second draft |
| Plan hash + `--confirm` | publishing something other than what you reviewed |

## Architecture in one paragraph

A canonical **Post IR** sits between your source and every platform. Adapters
declare **capabilities**; the planner intersects the two and emits explicit
**degradations**. A **runner** executes six steps per (document, platform) —
auth, draft, content, media, verify, publish — recording each in a sqlite state
store so a re-run resumes rather than restarts. Browser adapters inherit a DOM
toolkit that encodes everything above; API adapters inherit rate limiting and
retries. [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) has the diagram.

## Add a platform

```python
from pubkit.core.browser import BrowserAdapter, EditorSelectors
from pubkit.core.capabilities import Capabilities

class GhostAdapter(BrowserAdapter):
    name = "ghost"
    capabilities = Capabilities(tables=True, image_upload="browser_paste")
    selectors = EditorSelectors(
        editable=".koenig-editor__editor",
        content_roots=".koenig-editor__editor",   # ALL roots, not the first
        block="[data-kg='editor'] > *",
        figure="figure", figure_img="figure img", link="a",
        publish_button=".gh-publishmenu-trigger",
    )
    async def ensure_draft(self, doc, ctx): ...
    async def publish(self, doc, ref, ctx): ...
```

Register it from your own package — no fork required:

```toml
[project.entry-points."pubkit.adapters"]
ghost = "my_pubkit_ghost:GhostAdapter"
```

Five extension points, all first-class: `pubkit.adapters`, `pubkit.renderers`,
`pubkit.checks`, `pubkit.hooks`, and per-platform `transforms:` in config.

## Install it however you like

```bash
pip install "pubkit[all]"                       # the normal way
pipx install "pubkit[all]"                      # isolated CLI
uv tool install "pubkit[all]"                   # same, faster
docker run --rm -v "$PWD:/work" ghcr.io/arunsingh/pubkit validate content/
```

The Docker image ships Chromium and its system libraries, which is the part
nobody wants to install on a CI runner by hand.

As a GitHub Action, no install step at all:

```yaml
- uses: arunsingh/pubkit@v1
  with:
    command: plan
    path: content/
    platforms: medium,devto
```

## Fit it into a workflow

**GitHub Actions** — plan on every PR, publish on merge:

```yaml
- run: pubkit validate content/ --strict
- run: pubkit plan content/ --to medium,devto >> $GITHUB_STEP_SUMMARY
- run: pubkit publish content/ --to medium,devto --confirm
  if: github.ref == 'refs/heads/main'
  env:
    PUBKIT_VAULT_KEY: ${{ secrets.PUBKIT_VAULT_KEY }}
```

**Airflow** — one task per (document, platform), so retries and alerting belong
to Airflow:

```python
validate = PubkitValidateOperator(task_id="validate", path="content/series/")
validate >> PubkitPublishOperator.expand_fanout(
    path="content/series/", platforms=["medium", "devto", "x"], confirm=True,
)
```

**Library** — `from pubkit import Pipeline` for everything else.

## Security

- **pubkit never accepts a password.** Browser platforms use an interactive
  login you perform; pubkit persists only the session, encrypted at rest.
- API tokens live in the OS keychain, or an encrypted vault for CI.
- Secrets are stripped from logs by a filter, not by discipline.
- Adapters receive credentials for their own platform and nothing else.
- Every public write needs `--confirm`, against a hash-pinned plan.

## Status

v0.2. The core, the checks, the planner, the state machine, the browser pool
and the table renderer are covered by 49 tests, and the whole pipeline is
exercised end-to-end against a real published series in
`examples/inside-ai-infra/`. The browser adapters are additionally tested
against a deliberately hostile fake editor — multiple content roots, images
stripped from pasted HTML, figures landing above the caret, uploads that sit on
a `blob:` URL — which is where two real bugs were caught before release.

Selectors remain the part most likely to need a patch when a platform ships a
redesign, which is exactly why they are isolated in one dataclass per adapter.

## Docs

| | |
|---|---|
| [QUICKSTART.md](docs/QUICKSTART.md) | five minutes, one platform |
| [FAILURE-MODES.md](docs/FAILURE-MODES.md) | the 21 ways publishing quietly goes wrong |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | how it fits together |
| [ADAPTERS.md](docs/ADAPTERS.md) | add a platform |
| [SECURITY.md](SECURITY.md) | threat model and credential handling |

Contributions welcome — especially new adapters and new checks. The one hard
rule: a bug fix adds a row to `docs/FAILURE-MODES.md` and a test named after the
failure. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Licence

Apache 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
