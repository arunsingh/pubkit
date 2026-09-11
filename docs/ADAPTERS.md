# Writing an adapter

Six methods. Everything painful is already in the base classes, because those
are exactly the things that went wrong the first time and nobody should have to
rediscover them per platform.

Read [FAILURE-MODES.md](FAILURE-MODES.md) first. It is short, and it will save
you a weekend.

---

## Decide which kind you are writing

Does the platform have a documented write API?

- **Yes** → `ApiAdapter`. Roughly 80 lines. See `adapters/devto.py`.
- **No** → `BrowserAdapter`. Roughly 150 lines, and you mostly write selectors.
  See `adapters/substack.py`, which is the clean example; `adapters/medium.py`
  is the one with all the scar tissue.

## Declare capabilities honestly

```python
capabilities = Capabilities(
    tables=False,                    # Medium genuinely cannot do tables
    code_blocks="plain",
    animated_gif=True,
    headings=4,
    image_upload="browser_paste",
    tags=TagSpec(max_count=5, max_len=25, strategy="comma"),
)
```

This is the most important twenty lines you will write. An overstated
capability becomes a silent degradation at publish time — which is the entire
class of problem this design exists to eliminate. If you are unsure whether the
platform supports something, assume it does not and let the planner say so out
loud.

## The six methods

```python
async def authenticate(self, ctx) -> None:
    """Fail loudly and early. Never try to get past a login page."""

async def ensure_draft(self, doc, ctx) -> RemoteRef:
    """Reuse ctx.options['remote_ref'] when present. Never create a duplicate."""

async def push_content(self, doc, plan, ref, ctx) -> None:
    """Send the body. Content must be correct *before* it is sent."""

async def push_media(self, doc, plan, ref, ctx) -> None:
    """Images. Optional — the base class no-ops."""

async def verify(self, doc, plan, ref, ctx) -> Fingerprint:
    """Read state back from the remote. Not optional, not a courtesy."""

async def publish(self, doc, ref, ctx) -> PublishedRef:
    """Call self.guard_publish(ctx) first, always."""
```

## Rules that are not negotiable

**Every step must be safe to re-enter.** Assume the connection drops mid-run,
because it will. The runner resumes at the first incomplete step; if your
`ensure_draft` creates a new draft on every call, a retry silently doubles the
user's work.

**`verify()` must reload before it compares.** An editor will happily show you
a change it has not persisted. A delete that took a document from 355 to 189
paragraphs came back as 339 after a refresh. Ask the server what it actually
has.

**Never mutate the DOM to change content.** Setting `a.href` directly looks
like it worked and is gone on the next reload — the editor syncs from its own
model, not from your DOM edits. All content changes go through the platform's
own input path.

**`content_roots` must select every content root.** Selecting only the first is
how a paste that should have replaced 169 paragraphs replaced 2 and appended
the rest, producing a scrambled duplicate. `replace_document()` asserts the
resulting block count for exactly this reason.

**Never `execCommand('delete')` across the whole document.** It destroys the
editor's scaffolding; afterwards pastes land nowhere and only a reload recovers.

## Images in a browser adapter

The one path that works:

1. Render figures as **caption-only** paragraphs. No `<img>`, no `data:` URI, no
   remote URL — all three are stripped from pasted HTML.
2. Inject an `<input type=file>` and hand it real bytes. Never click a file
   input; that opens a native picker you cannot drive and it blocks the session.
3. Place a **collapsed caret** at the end of the caption paragraph.
4. Dispatch a `ClipboardEvent` whose `DataTransfer.items` contains the `File`.
   The editor runs its own uploader and the image lands on the platform CDN.
5. Wait until the figure exists **and** its `src` is no longer a `blob:` URL.

The figure lands *above* the caret's paragraph, so anchoring on the caption
gives the right visual result for free. Re-resolve anchors after every insert —
indices shift.

`BrowserAdapter.insert_image_at()` does all of this. You should not need to
reimplement it; if you do, something about your platform is worth a new row in
FAILURE-MODES.md.

## Tags

Tag widgets look like text inputs and are not. Typing `a,b,c` into one produced
a single 40-character invalid tag. `apply_tags()` supports three strategies —
`comma`, `enter`, `native_setter` — and **asserts the chip count afterwards**.
If none commit, it clears the field and publishes without tags rather than
shipping one malformed tag. Pick the strategy by experiment, not by guessing.

## Rate limits

Set `rate` and `burst` from the platform's documented limits, not from what you
can get away with. Read `retry-after`; if the platform sends an epoch rather
than a delta — X does — convert it, or you will sleep until 2056.

## Register it

From your own package. No fork needed:

```toml
[project.entry-points."pubkit.adapters"]
ghost = "my_pubkit_ghost:GhostAdapter"
```

## Test it

Mirror `tests/test_core.py`: name each test after the failure it prevents. A
test called `test_paste_replaces_rather_than_appends` tells the next maintainer
why the odd-looking assertion in `replace_document()` is load-bearing. A test
called `test_push_content` tells them nothing.

For browser adapters, test the pure parts — rendering, anchors, capability
planning — as unit tests, and keep the live-editor work behind an opt-in
integration marker. Selectors will drift; the logic should not.
