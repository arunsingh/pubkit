# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning is [semantic](https://semver.org/); adapters may change behaviour on
a minor bump when a platform changes underneath them.

## [0.1.0] — 2026-09-11

First release. Extracted from a real, painful publishing run: a 15,000-word
illustrated three-part series to Medium that surfaced 21 distinct failure modes,
all catalogued in `docs/FAILURE-MODES.md`.

### Added

- **Post IR** — a canonical, platform-agnostic document model with a
  content-addressed id used as the idempotency key everywhere.
- **Capability negotiation** — adapters declare what they support; the planner
  emits explicit, printable degradations. `pubkit plan` shows them before
  anything is sent.
- **Check pipeline** — placeholders, numeric assertions, cross-references,
  number drift, assets, budget, structure. Pluggable via `pubkit.checks`.
- **Verified chunked transport** — per-chunk rolling-hash verification, probed
  size ceilings, single-member gzip.
- **Browser adapter toolkit** — paste-based document replace with a block-count
  assertion, reload-then-fingerprint verification, caption-anchored `File`
  image insertion, Unicode-folding anchor resolution, tag-chip post-conditions.
- **Adapters** — Medium, Substack (browser); X, Dev.to, Hashnode (API).
- **Runner** — six resumable steps per (document, platform), sqlite state store,
  two-phase publish for series, hash-pinned `--confirm` gate, per-platform rate
  limiting and jittered retry.
- **CLI** — `init`, `validate`, `plan`, `publish`, `status`, `platforms`,
  `doctor`, `auth login|logout|list`.
- **Workflows** — GitHub Actions examples and Airflow operators.
- **Extension points** — `pubkit.adapters`, `pubkit.renderers`, `pubkit.checks`,
  `pubkit.hooks`, plus per-platform transforms.

### Known limitations

- Browser adapter selectors will need patching when a platform redesigns its
  editor. They are isolated in one `EditorSelectors` dataclass per adapter
  precisely so that is a small change.
- Table→image rendering requires the `render` extra; without it, tables degrade
  to lists on platforms that lack table support.
- X media upload uses the v1.1 endpoint, which is what the v2 API still
  requires.
