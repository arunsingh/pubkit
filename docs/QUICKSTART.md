# Quickstart

Five minutes, one platform, nothing published until you say so.

## 1. Install

```bash
pip install "pubkit[all]"
playwright install chromium     # Medium and Substack only
pubkit doctor                   # confirms the environment before you start
```

## 2. Scaffold

```bash
pubkit init my-blog && cd my-blog
pubkit validate content/
```

`init` writes a sample post that already passes every check, a `pubkit.toml`,
and a GitHub Actions workflow that plans on PRs and publishes on merge.

## 3. Look before you leap

```bash
pubkit plan content/ --to medium,devto
```

You get the degradations spelled out — tables becoming images, tags being
trimmed, a body being split into a thread. Nothing has been sent.

## 4. Sign in

```bash
pubkit auth login devto     # paste a token → OS keychain
pubkit auth login medium    # a window opens; you sign in; pubkit keeps the session
```

pubkit never asks for a password. For browser platforms you log in yourself,
MFA and all, and only the session is persisted (encrypted).

## 5. Draft, then publish

```bash
pubkit publish content/ --to medium,devto            # drafts only
pubkit publish content/ --to medium,devto --confirm  # live
pubkit status                                        # what exists where
```

Both are safe to re-run. A dropped connection costs you one step, not one run,
and a second run updates rather than creating a duplicate.

---

## Writing content

Front-matter plus Markdown. Everything below is optional except `title`.

```markdown
---
id: my-post
title: "A title: with a colon needs quotes"
subtitle: One line on what the reader gets.
tags: [Engineering, Python]
canonical_url: https://myblog.com/my-post
budget: {words: 1500, tolerance: 0.15}
series: {id: my-series, index: 1, of: 3}
---
```

### Numbers the build checks

```markdown
<!-- pubkit:define nvlink = 900 -->
<!-- pubkit:define pcie5 = 128 -->

NVLink is 7× wider. <!-- pubkit:assert 7x = nvlink/pcie5 ±10% -->
```

If you later edit one of those numbers and forget the other, the build fails
instead of the internet noticing.

### Tables, figures, callouts

```markdown
| Model | Size |
| :--- | ---: |
| 70B | 140 GB |
*Caption becomes the figure caption when this is rendered as an image*

![alt text](img/diagram.gif)
*The caption is also the anchor pubkit uses to place the image*

> [!key] Design rule
> Callouts become the best tweets in your promo thread, for free.
```

### Series cross-links

```markdown
Continued in [Part 2](${series.part2.url}).
```

Resolved automatically during a two-phase publish: every draft is created
first, permalinks are collected, then the final content is pushed. You never
have to publish part 1, copy the URL, and edit part 2 by hand.

## Where things live

```
.pubkit/
  state.sqlite     what is drafted/published where — makes re-runs safe
  sessions/        encrypted browser sessions
  vault.json       encrypted token fallback when there is no OS keychain
```

Gitignored by the scaffold. Deleting it is safe; you will just re-authenticate
and pubkit will re-detect existing drafts on the next run.

## Next

- [ADAPTERS.md](ADAPTERS.md) — add a platform
- [FAILURE-MODES.md](FAILURE-MODES.md) — what this actually guards against
- [ARCHITECTURE.md](ARCHITECTURE.md) — how it fits together
