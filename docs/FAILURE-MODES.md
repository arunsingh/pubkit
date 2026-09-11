# Failure modes of automated publishing

Every entry below is a real failure observed while publishing a three-part,
15,000-word illustrated series to Medium. Each one is the reason a specific
component exists in pubkit. This file is the spec; the code is the answer.

A useful framing: **publishing is a distributed system.** You have an
unreliable channel (a browser bridge, or an HTTP API behind a rate limiter),
a stateful remote (a draft that may already be half-written), and a
non-idempotent operation (publish). Everything below follows from that.

---

## Class A — Transport: the channel corrupts or truncates your payload

### A1. Silent single-character corruption in transit
A 12,368-character payload arrived with one byte mutated (`h` 104 → `g` 103).
gzip failed downstream with `invalid distance too far back`. Nothing reported
an error; the channel simply lied.

> **pubkit:** every chunk carries a 32-bit rolling hash computed at the source
> and re-computed at the destination. `transport.py::verified_push()` refuses
> to assemble a payload whose hash does not match, and reports the chunk index.

### A2. Payload-size ceilings that are never documented
The bridge failed with `Failed to fetch` above roughly 6.5 KB per call, and a
separate, *different* ceiling applied to how much could be emitted per message.
Both were discovered only by hitting them.

> **pubkit:** `ChunkedTransport` probes the ceiling once with a binary search,
> caches it per channel in the state store, and never assumes a constant.

### A3. Loss of in-memory state on navigation or reload
Decompressed payloads held in `window.__H` vanished on every navigation.

> **pubkit:** payloads are staged in `sessionStorage` (survives navigation
> within an origin) and re-materialised on demand; the stage is content-
> addressed so a re-push is a no-op.

### A4. Multi-member gzip streams
Two independently-gzipped halves concatenated fine in Python (`gzip.decompress`
handles multi-member) but `DecompressionStream('gzip')` in the browser threw
`Junk found after end of compressed data`.

> **pubkit:** one member per payload, always. `codec.py` asserts a single
> gzip member before transmission.

---

## Class B — The editor is a hostile, stateful remote

### B1. Paste inserts rather than replaces
Replacing a document by selecting all paragraphs and dispatching a synthetic
paste **appended** instead, producing a scrambled document with the old content
duplicated after the new. Root cause: `querySelector('.section-inner')` grabbed
only the *first* section, so the range covered 2 of 169 paragraphs.

> **pubkit:** `browser/dom.py::select_whole_document()` builds the range across
> *all* content roots, and `replace_document()` asserts
> `paragraphs_after == expected_paragraph_count` before returning. A mismatch
> raises `DocumentReplaceMismatch` and triggers the reload-and-retry path.

### B2. `execCommand('delete')` over the full document destroys editor scaffolding
Deleting every paragraph left the editor structurally broken; subsequent pastes
silently did nothing. Recovery required a reload to restore the server-side copy.

> **pubkit:** never delete the whole document. Replace by paste-over-selection;
> delete only bounded sub-ranges. `NEVER_DELETE_ALL` is enforced in code.

### B3. Deletions do not always persist
A delete that appeared to work (355 → 189 paragraphs) came back as 339 after a
reload — the editor's delta sync had dropped it.

> **pubkit:** every mutating step is followed by a **reload-and-verify** pass
> against a content fingerprint (word count, heading list, image count, marker
> count). `verify.py::assert_fingerprint()`. Mutations are not trusted until
> they survive a round-trip.

### B4. Direct DOM mutation does not persist at all
Setting `a.href` fixed the links on screen; after a reload every one was back to
the placeholder. Medium's editor keeps its own model and ignores DOM edits.

> **pubkit:** never mutate the DOM to change content. All content changes go
> through the platform's own input path (paste/typing events), which is what
> its model listens to. Rendering is done **before** transmission, not after.

### B5. Remote images are stripped from synthetic paste events
Both `data:` URIs and ordinary `https://` image URLs were removed from pasted
HTML — proven by pasting a live Wikimedia URL and watching it vanish while the
adjacent text marker survived.

> **pubkit:** images are never embedded in the pasted HTML. They are uploaded
> as real `File` objects and inserted separately (see B6).

### B6. The only reliable image path is a synthetic `File` paste
After eight dead ends — `data:` URI, remote URL, `File` in `clipboardData` (an
early, wrongly-constructed attempt), synthetic `DragEvent`, hidden file input,
`upload-by-URL` endpoints, OS-level drag — the technique that works is:
place a collapsed caret, then dispatch a `ClipboardEvent` whose
`DataTransfer.items` contains a `File`. The editor's own upload path runs and
the image lands on the platform CDN.

> **pubkit:** `browser/media.py::insert_image_at()` implements exactly this,
> plus the `<input type=file>` injection trick that lets an external automation
> layer hand real bytes to the page without ever opening a native picker.

### B7. The figure lands *before* the caret's paragraph
Inserting at the end of paragraph *N* places the figure at index *N*, pushing
that paragraph to *N+1*.

> **pubkit:** captions are rendered as the anchor. You target the caption
> paragraph and the image arrives directly above it — which is also the correct
> visual result. Anchors are re-resolved by text after every insert, because
> indices shift.

### B8. Unicode normalisation breaks text anchors
An anchor lookup failed because the rendered text contained `U+200A HAIR SPACE`
and `U+2014 EM DASH` where the source had `" - "`. Exact string equality is a
trap.

> **pubkit:** `anchors.py::normalise()` folds whitespace classes, dashes,
> quotes and NBSP before matching, and falls back to a prefix match, then to a
> fuzzy ratio with a configurable floor.

### B9. Tag/topic inputs that look like text fields but are not
Typing `A,B,C` into Medium's Topics field produced one invalid 40-character
tag. Both `Enter` and `,` failed to commit a chip because synthetic key events
did not drive the framework's handler.

> **pubkit:** `TagStrategy` per adapter, with three implementations
> (`comma`, `enter`, `native_setter`) and a post-condition assert on the chip
> count. If no strategy commits, publishing proceeds **without** tags and warns,
> rather than publishing a broken tag string.

---

## Class C — Content correctness, not mechanics

### C1. Numeric claims drift as text is edited
A late edit transposed two ratios: "18× between NVLink and PCIe, ~50× to the
network" when the correct figures are **7×** and **18×**. Two sections also
disagreed on a throughput number (5,000 vs 5,150 tok/s), which propagated into
three cost figures.

> **pubkit:** `checks/numeric.py` extracts every `<number> <unit>` and every
> stated ratio from the IR, re-derives the ones that are computable from
> declared source values, and fails the build on a mismatch. 32 claims were
> checked this way; 5 were wrong; all 5 were real.

### C2. Cross-references break when one document is split into many
Seven phrases like "as we saw in Part 4" became wrong the moment the article
became three articles, and "Part 1" meant two different things (series part vs
section number).

> **pubkit:** the IR models a `Series`. `checks/xref.py` resolves every
> cross-reference to a `(document, section)` pair and fails on an unresolvable
> or self-contradictory one. Section numbering is generated, never hand-written.

### C3. Placeholder links reaching production
`URL-PART-2` links existed before the target document had a URL — a classic
chicken-and-egg in any multi-document publish.

> **pubkit:** two-phase publish. Phase 1 creates every draft and collects its
> permalink; phase 2 resolves `${series.part2.url}` templates and pushes the
> final content. `checks/placeholders.py` blocks publish while any unresolved
> token remains.

### C4. Length targets missed silently
The first draft came in at 16,388 words against a 12,000-word target.

> **pubkit:** `budget` constraints in front-matter (`words: 12000 ±15%`), checked
> in `pubkit validate`.

### C5. Character-escaping bugs in the correction pipeline
A find-and-replace table failed silently because `×` was mangled by Python
source escaping.

> **pubkit:** corrections live in UTF-8 JSON (`ensure_ascii=False`), never in
> source literals, and every correction asserts it matched exactly once.

---

## Class D — Operational reality

### D1. The automation bridge disconnects mid-run
The MCP bridge dropped ~8 times; the Chrome extension dropped mid-publish; a
safety classifier timed out and blocked tool calls for minutes.

> **pubkit:** every step is **resumable**. A sqlite state store records
> `(document, platform, step, fingerprint)`. Re-running `pubkit publish` skips
> completed steps and resumes at the first incomplete one. No step is
> destructive on re-entry.

### D2. Non-idempotent publish
Re-running a naive publisher creates duplicate drafts and, worse, duplicate
published posts.

> **pubkit:** every document has a stable `content_id` (hash of canonical IR).
> Adapters record the remote id against it. A second run **updates** rather
> than creates. `--force-new` is explicit.

### D3. Irreversible actions taken without a human in the loop
Publishing is public and hard to undo.

> **pubkit:** `publish` is gated. Default is `--dry-run`-style planning; going
> live needs `--confirm` (or `PUBKIT_CONFIRM=1` in CI), and the plan that gets
> confirmed is hash-pinned, so the content cannot change between plan and apply.

### D4. Credentials in the wrong place
The right answer is never to handle them at all.

> **pubkit:** the tool **never accepts a password**. API tokens go to the OS
> keychain. For browser platforms the user logs in themselves in a visible
> browser window; pubkit persists an encrypted Playwright `storage_state` and
> reuses it. `pubkit auth login <platform>` opens the window and waits.

### D5. Rate limits and partial failure across platforms
Publishing one document to five platforms is a fan-out where any leg can fail.

> **pubkit:** per-platform token-bucket limiter, bounded exponential backoff
> with jitter, and per-leg result reporting. One platform failing never blocks
> the others; the run exits non-zero with a machine-readable summary.

---

## Class E — Sign-in

A login is the only step a human performs and a machine has to judge, and every
failure in it is silent. These six were all observed against live platforms.

### E1. The submit button does nothing at all
A sign-in page opens, the email is typed, Continue is clicked, and nothing
happens. No error, no navigation, no network request, nothing in the console.
It is the hardest login failure to report because from the outside there is
nothing to report.

> **pubkit:** the page is watched while the human works. Every POST/fetch to
> the platform is counted, so "you did not finish" and "the button never fired"
> stop being the same silence. Zero submissions after a timeout is reported as
> a failure in its own right, with `--attach` as the next step.

### E2. A bot check that never completes
Identical symptom to E1, opposite cause: the click *is* handled, but the
handler waits on a challenge script that a freshly launched browser profile
never gets a token from. Indistinguishable by eye.

> **pubkit:** requests to known challenge hosts (reCAPTCHA, hCaptcha, Arkose,
> PerimeterX, DataDome, Turnstile) are recorded separately, and a failed one is
> named in the diagnosis. The remedy is `pubkit auth login <platform> --attach`,
> which watches a Chrome the user started themselves. pubkit does not attempt
> to defeat a challenge; it gets out of the way of one.

### E3. Waiting five minutes for something that could never happen
playwright not installed, no browser downloaded, the login host unreachable
behind a VPN or proxy, a session directory that is not writable. Each produces
the same blank window and the same long wait.

> **pubkit:** `login_preflight()` runs first, synchronously and cheaply. A
> window is never opened when it cannot work, and each failure names its own
> fix. It also says when a valid session already exists, so the user is not
> signed out of something that was working.

### E4. The flow stops at the identity provider
Sign-in goes through Google or a corporate SSO hop, the redirect back never
completes, and what gets saved is a session for `accounts.google.com` with no
cookie for the platform at all.

> **pubkit:** the saved state is checked for cookies on the platform's own
> registrable domain, and for the specific cookie names that authorise writing.
> Missing ones are named.

### E5. A session that validates and does not work
Both session cookies present, correct domain, good expiry — and the signed-in
page still bounces back to sign-in. Cookie inspection cannot detect this; only
asking the platform can.

> **pubkit:** before anything is written, a page that renders only for a
> signed-in user is fetched and checked for a signed-in marker. A session is
> saved only once it has been *proven* to work. A saved session that does not
> work is worse than none, because it moves the failure into the middle of a
> publish.

### E6. A session that expires quietly
A session cookie with no persistent expiry dies with the browser; one with a
two-day expiry dies mid-week. Either way the failure surfaces days later,
inside a publish, looking like something else.

> **pubkit:** expiry is reported at login time, and a lifetime under a week is
> a warning with the number in it. `pubkit auth verify` re-runs the identical
> checks on demand — same code path, so a session cannot pass one and fail the
> other.
