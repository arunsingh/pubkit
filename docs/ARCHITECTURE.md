# Architecture

```
                     ┌───────────────────────────────────────────┐
  post.md  ─────────▶│  Loader          front-matter + body       │
  assets/            └───────────────────┬───────────────────────┘
                                         ▼
                     ┌───────────────────────────────────────────┐
                     │  Post IR   canonical, platform-agnostic   │
                     │  Document · Block[] · Asset[] · Series    │
                     └───────────────────┬───────────────────────┘
                                         ▼
                     ┌───────────────────────────────────────────┐
                     │  Check pipeline   (fail fast, pre-flight) │
                     │  numeric · xref · placeholders · budget   │
                     │  assets · frontmatter · link-health       │
                     └───────────────────┬───────────────────────┘
                                         ▼
                     ┌───────────────────────────────────────────┐
                     │  Planner    capability negotiation        │
                     │  IR ∩ Adapter.capabilities → PublishPlan  │
                     │  degradations: table→image, html→md, …    │
                     └───────────────────┬───────────────────────┘
                                         ▼
        ┌──────────────┬─────────────────┼────────────────┬──────────────┐
        ▼              ▼                 ▼                ▼              ▼
   ┌─────────┐   ┌──────────┐      ┌──────────┐    ┌──────────┐   ┌──────────┐
   │ Medium  │   │ Substack │      │    X     │    │  Dev.to  │   │ Hashnode │
   │ browser │   │ browser  │      │ API v2   │    │   API    │   │  GraphQL │
   └────┬────┘   └────┬─────┘      └────┬─────┘    └────┬─────┘   └────┬─────┘
        └─────────────┴─────────────────┴───────────────┴──────────────┘
                                         │
                     ┌───────────────────▼───────────────────────┐
                     │  Runner   idempotent · resumable · gated   │
                     │  StateStore (sqlite) · RateLimiter · Hooks │
                     └───────────────────────────────────────────┘
```

## 1. The Post IR

Everything hinges on one idea: **never let a platform's quirks reach your
source, and never let your source assume a platform.** Between them sits a
canonical intermediate representation.

```python
Document(
    id="inside-ai-infra-p1",          # stable, author-assigned
    title=..., subtitle=..., tags=[...],
    blocks=[Heading, Paragraph, Code, Quote, List, Table, Figure, Embed, Rule],
    assets={"fig-01": Asset(path=..., alt=..., caption=...)},
    series=SeriesRef(id="inside-ai-infra", index=1, of=3),
)
```

`content_id` = BLAKE2b over the canonical serialisation. It is the idempotency
key for every adapter, and the hash that pins a plan to its content.

Two properties matter:

- **Blocks are semantic, not visual.** A `Table` is a table, not a PNG. The
  decision to render it as an image is taken by the planner, per platform,
  because Medium cannot do tables and Dev.to can.
- **Figures reference assets by id, not path.** The same `Figure` becomes an
  uploaded CDN image on Medium, a Markdown `![]()` on Dev.to, and an attached
  media id on X. The IR does not know or care.

## 2. Capability negotiation

Each adapter declares what it can do:

```python
class Capabilities(BaseModel):
    tables: bool
    code_blocks: Literal["fenced", "highlighted", "none"]
    inline_html: bool
    animated_gif: bool
    max_body_chars: int | None
    image_upload: Literal["api", "browser_paste", "none"]
    canonical_url: bool
    tags: TagSpec | None
    scheduling: bool
    threads: bool          # X
```

The planner intersects the IR with these and emits a `PublishPlan` containing
explicit, reviewable **degradations**:

```
medium:  Table×8 → rendered PNG figures (platform lacks tables)
         Figure×15 → browser_paste upload
x:       body 15,430 chars → 9-tweet thread; Figure×2 attached to tweets 1,4
devto:   native tables, native code fences — no degradation
```

`pubkit plan` prints this. Nothing is a surprise at publish time.

## 3. The adapter contract

```python
class Adapter(Protocol):
    name: str
    capabilities: Capabilities

    async def authenticate(self, ctx: Context) -> None: ...
    async def ensure_draft(self, doc: Document, ctx: Context) -> RemoteRef: ...
    async def push_content(self, doc, plan, ref, ctx) -> None: ...
    async def push_media(self, doc, plan, ref, ctx) -> None: ...
    async def verify(self, doc, plan, ref, ctx) -> Fingerprint: ...
    async def publish(self, doc, ref, ctx) -> PublishedRef: ...
```

Six methods. That is the whole surface area. `verify()` is not optional and not
a courtesy — it is what makes B3 (deletions that don't persist) survivable.

Two base classes do the heavy lifting:

- **`ApiAdapter`** — httpx client, token from keyring, token-bucket limiter,
  retry with jitter, typed errors.
- **`BrowserAdapter`** — Playwright context with persisted `storage_state`,
  plus the DOM toolkit that encodes Class B: `replace_document()`,
  `insert_image_at()`, `resolve_anchor()`, `assert_fingerprint()`,
  `ChunkedTransport`.

A new browser platform is roughly 150 lines: selectors, a tag strategy, and a
publish click. A new API platform is roughly 80.

## 4. Idempotency and resumption

```
state.sqlite
  runs(run_id, plan_hash, created_at, status)
  steps(run_id, document_id, platform, step, status, fingerprint, remote_ref)
```

`publish` walks the step list per (document, platform). A step whose recorded
fingerprint still matches the remote is skipped. A bridge that dies mid-run
costs you one step, not one run. Nothing in the step list is destructive when
re-entered — that is a design constraint on adapter authors, and it is tested.

## 5. Two-phase publish, for series

Phase 1 creates drafts everywhere and records permalinks. Phase 2 resolves
`${series.part2.url}` and pushes final content. This is the only correct way to
publish mutually-linking documents, and it falls straight out of C3.

## 6. Extension points

Five, all first-class:

| Point | Mechanism | Use it for |
|---|---|---|
| Adapters | `pubkit.adapters` entry point | new platforms |
| Renderers | `pubkit.renderers` entry point | your own table/diagram look |
| Checks | `pubkit.checks` entry point | house style, legal review, SEO |
| Hooks | `pubkit.hooks` entry point | Slack notify, analytics, git tag |
| Transforms | `transforms:` in config | per-platform CTA, UTM tagging |

Hooks fire on `pre_check`, `post_plan`, `pre_publish`, `post_publish`,
`on_error`. `pre_publish` may veto — that is how you wire an approval gate.

## 7. Workflow integrations

- **CLI** — `pubkit plan|publish|status|auth|validate`
- **GitHub Action** — publish on merge to `main`, plan on PR with the plan
  posted as a PR comment
- **Airflow** — `PubkitPublishOperator`, one task per (document, platform), so
  retries and alerting are Airflow's problem, not ours
- **Library** — `from pubkit import Pipeline` for anything else

## 8. Security posture

- No password ever enters the tool. Browser platforms use an
  interactive login the user performs; pubkit persists the session only.
- `storage_state` is encrypted at rest (Fernet, key in the OS keychain).
- Secrets are redacted from logs by a filter, not by discipline.
- Adapters get a `Context` with narrowly-scoped credentials for their platform
  only.
- `--confirm` is required for any public write. CI must set it explicitly.
