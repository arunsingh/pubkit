# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning is [semantic](https://semver.org/); adapters may change behaviour on
a minor bump when a platform changes underneath them.

## [0.3.0] — 2026-09-11

Sign-in is the one step a human performs and a machine has to judge, and every
way it fails is silent. This release makes each of them say something.

### Added

- **`login_preflight()`** — playwright, browser, host reachability, a writable
  session store and any existing session are checked before a window opens.
  A login that cannot work no longer costs five minutes to find out.
- **Live observation during sign-in** — console errors, failed requests and
  challenge-host scripts are recorded while the human works, and submissions to
  the platform are counted. A button that never fires is now distinguishable
  from a bot check that never completes; previously both were silence.
- **Post-flight proof** — the saved state is checked for cookies on the
  platform's own domain and for the cookie names that authorise writing, its
  expiry is reported, and a signed-in page is fetched and checked. **A session
  is written only after it has been shown to work.**
- **`pubkit auth login <platform> --attach`** — watch a Chrome you started
  yourself (`--remote-debugging-port=9222`) instead of launching one, for
  platforms that will not complete a sign-in in a fresh profile.
- **`pubkit auth login --minutes`** to set the wait.
- **`core/login.py`** — one `LoginFlow` per platform declaring success markers,
  required cookies, cookie domain, probe URL and signed-in markers. A new
  platform inherits the whole validation suite rather than new special cases.
- **`tests/fixtures/login_server.py`** — a sign-in page that fails the six ways
  real ones do: dead button, blocked challenge, partial cookies, short-lived
  session, ghost session, and the happy path. 26 tests cover the matrix; the
  unit half needs no browser at all.
- **Class E in `docs/FAILURE-MODES.md`** — six new entries.

### Changed

- A launched login window now uses a **persistent pubkit profile** and real
  Google Chrome where it is installed, falling back to bundled Chromium.
  pubkit does not try to disguise an automated browser; where a platform
  declines one, `--attach` uses the browser you actually sign in with.
- `pubkit auth verify` runs the identical post-flight checks as `login`, so a
  session cannot pass one and fail the other, and prints each finding.
- `BROWSER_PLATFORMS` derives from the login flow table — one source of truth.

## [0.2.0] — 2026-09-11

The release that makes `pubkit publish --to medium --confirm` actually work
unattended. 0.1.x could plan a Medium publish; it could not perform one.

### Added

- **Browser lifecycle** (`browserctl.BrowserPool`, `attached()`). This was the
  missing seam: the runner called `adapter.authenticate()` on a `MediumAdapter`
  whose `_page` was still `None`. Now one browser serves the whole run, one
  context per platform so a Medium session is never presented to Substack, and
  sessions are refreshed on the way out because cookies rotate. API-only runs
  never launch Chromium at all.
- **Table rendering** (`render.tables`, `core.assets.materialise`). The planner
  said `table_to_image` and listed the asset ids; nothing produced the files.
  Tables now render to quantised PNGs at 2x, cached on cell content so a re-run
  neither re-renders nor re-uploads an unchanged table. The source IR is never
  mutated — the same document keeps native tables on Dev.to in the same run.
- `pubkit auth verify <platform>` — check a session before a publish depends on
  it, rather than discovering it expired halfway through.
- **Browser integration tests** against a deliberately hostile fixture editor
  that reproduces multiple content roots, image stripping, figures landing
  above the caret, and uploads that sit on a `blob:` URL before resolving.
  They skip cleanly where Chromium is unavailable.

### Fixed

- `marker_regex` was double-escaped, so every `fingerprint()` call threw
  `SyntaxError: Invalid regular expression` inside the page. Verification was
  therefore broken on every browser adapter. Found by the new integration
  tests on their first run.
- `auth login` routed on a capability flag rather than the platform list, so
  `pubkit auth login substack` took the API-token path and prompted for a token
  that does not exist.

## [0.1.1] — 2026-09-11

### Added

- Docker image on the Playwright base (`ghcr.io/arunsingh/pubkit`), so browser
  adapters work in CI without hand-installing Chromium's system libraries.
- `action.yml` — the repository is now a reusable GitHub Action.
- `CITATION.cff`, and a Homebrew formula template under `packaging/`.

### Fixed

- `pubkit doctor` ignored `PLAYWRIGHT_BROWSERS_PATH` and reported Chromium as
  missing on managed images (CI runners, devcontainers, the Playwright Docker
  image) where it was in fact installed — sending people off to fix a problem
  they did not have. Found by running the published package in exactly such an
  environment.

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
