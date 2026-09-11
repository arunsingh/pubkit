# Copyright 2026 The pubkit Authors
# SPDX-License-Identifier: Apache-2.0
"""X / Twitter, API v2.

The interesting problem here is not the API, it is that a 6,000-word article
and a 280-character post are different media. The ThreadRenderer does the
splitting; this adapter deals with the parts that bite:

  * media upload still lives on the v1.1 host and needs its own flow
  * `x-rate-limit-reset` is an epoch, not a delta
  * a thread is a chain — a failure at tweet 7 of 11 leaves a visible,
    half-finished thread, so the chain is checkpointed per tweet and resumable
  * posting is irreversible and instantly public, so the publish guard matters
    more here than anywhere else
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path

from ..core.adapter import AdapterError, Context, Fingerprint, PublishedRef, RemoteRef
from ..core.capabilities import Capabilities
from ..core.ir import Document
from ..render.html import ThreadRenderer, plain
from .api_base import ApiAdapter

log = logging.getLogger(__name__)


class XAdapter(ApiAdapter):
    name = "x"
    base_url = "https://api.twitter.com"
    rate = 0.2          # v2 free/basic tiers are stingy; be a good citizen
    burst = 2

    capabilities = Capabilities(
        tables=False,
        code_blocks="none",
        inline_html=False,
        animated_gif=True,
        headings=1,
        max_body_chars=280,
        # promo mode never needs more than max_posts; see __init__.
        image_upload="api",
        canonical_url=False,
        tags=None,           # hashtags are body text here, not metadata
        threads=True,
        drafts=False,        # no draft concept — publish is one-shot
    )

    def __init__(self, limit: int = 280, mode: str = "promo", max_posts: int = 12) -> None:
        """`mode`:

        promo (default)
            A hook, three or four of the article's sharpest claims, and a link.
            This is what actually works on X, and it is what a long-form author
            wants: the article lives on the blog, the thread sells it.

        full
            Serialise the whole body. Honest, occasionally right, usually a
            180-tweet wall nobody reads. Opt in deliberately.
        """
        super().__init__()
        self.mode = mode
        self.max_posts = max_posts
        self.renderer = ThreadRenderer(limit=limit)

    def refine_plan(self, doc: Document, p):
        """promo mode summarises rather than serialises, so the generic
        'body is too long' degradation is simply wrong here."""
        from ..core.capabilities import Degradation, DegradationKind

        if self.mode != "promo":
            return p
        n = len(self._thread(doc))
        p.degradations = [
            d for d in p.degradations
            if d.kind not in (DegradationKind.SPLIT_INTO_THREAD, DegradationKind.CODE_FLATTENED,
                              DegradationKind.HEADINGS_CLAMPED)
        ]
        p.blocking = [b for b in p.blocking if "body is" not in b]
        p.degradations.insert(0, Degradation(
            kind=DegradationKind.SPLIT_INTO_THREAD,
            detail=f"promo thread: {n} posts — hook, key claims, link to the full article",
            count=n,
        ))
        p.estimated_body_chars = sum(len(t) for t in self._thread(doc))
        return p

    def headers(self, ctx: Context) -> dict[str, str]:
        return {
            "authorization": f"Bearer {ctx.tokens.require(self.name, 'bearer')}",
            "content-type": "application/json",
        }

    async def authenticate(self, ctx: Context) -> None:
        await self.request(ctx, "GET", "/2/users/me")

    def _thread(self, doc: Document) -> list[str]:
        if self.mode == "full":
            return self.renderer.render(doc)[: self.max_posts * 4]
        return self._promo(doc)

    def _promo(self, doc: Document) -> list[str]:
        """Hook, the strongest claims, then the link."""
        from ..core.ir import Callout, ListBlock, Paragraph

        link = doc.canonical_url or ""
        posts: list[str] = []

        opener = doc.subtitle or next(
            (b.text for b in doc.blocks if isinstance(b, Paragraph)), doc.title
        )
        posts.append(f"{doc.title}\n\n{plain(opener)}"[:265])

        # Callouts and short standalone list items are the claims an author
        # already marked as load-bearing; they make far better tweets than
        # arbitrary paragraph slices.
        claims: list[str] = []
        for b in doc.blocks:
            if isinstance(b, Callout):
                claims.append(plain(b.text))
            elif isinstance(b, ListBlock):
                claims.extend(plain(i) for i in b.items if 40 <= len(i) <= 230)
            if len(claims) >= self.max_posts - 2:
                break
        for c in claims[: self.max_posts - 2]:
            posts.append(c[:265])

        posts.append(("Full write-up: " + link).strip() if link else "Full write-up in the replies.")
        total = len(posts)
        return [f"{p} ({i+1}/{total})" if total > 1 else p for i, p in enumerate(posts)]

    async def ensure_draft(self, doc: Document, ctx: Context) -> RemoteRef:
        # No drafts on this platform. The "draft" is the rendered thread, which
        # we hold locally so `plan` can show it before anything is posted.
        return RemoteRef(id=f"local:{doc.content_id[:12]}", extra={"thread": self._thread(doc)})

    async def push_content(self, doc, plan, ref, ctx) -> None:
        ref.extra["thread"] = self._thread(doc)

    async def push_media(self, doc, plan, ref, ctx) -> None:
        """Upload the first few figures and pin them to early tweets.

        Deliberately not all of them: a thread where every tweet carries an
        image reads as a slideshow and performs worse than one good visual on
        the opening post.
        """
        figures = doc.figures[:4]
        media_ids: list[str] = []
        for fig in figures:
            asset = doc.assets[fig.asset_id]
            media_ids.append(await self._upload_media(ctx, asset.path))
        ref.extra["media_ids"] = media_ids

    async def _upload_media(self, ctx: Context, path: Path) -> str:
        data = base64.b64encode(path.read_bytes()).decode()
        await self.bucket.acquire()
        client = await self.client(ctx)
        r = await client.post(
            "https://upload.twitter.com/1.1/media/upload.json",
            data={"media_data": data},
            headers={"authorization": client.headers["authorization"]},
        )
        if r.status_code >= 400:
            raise AdapterError(f"x: media upload failed {r.status_code} {r.text[:200]}")
        return str(r.json()["media_id_string"])

    async def verify(self, doc, plan, ref, ctx) -> Fingerprint:
        thread = ref.extra.get("thread", [])
        over = [i for i, t in enumerate(thread) if len(t) > 280]
        if over:
            raise AdapterError(f"tweets {over} exceed 280 characters after rendering")
        return Fingerprint(
            words=sum(len(t.split()) for t in thread),
            headings=[],
            images=len(ref.extra.get("media_ids", [])),
            links=sum(t.count("http") for t in thread),
        )

    async def publish(self, doc, ref, ctx) -> PublishedRef:
        """Post the chain, checkpointing after every tweet.

        If tweet 7 of 11 fails, the already-posted 6 are recorded so a resumed
        run continues the chain instead of starting a second one beside it.
        """
        self.guard_publish(ctx)
        thread: list[str] = ref.extra["thread"]
        media_ids: list[str] = ref.extra.get("media_ids", [])
        posted: list[str] = ref.extra.setdefault("posted", [])

        reply_to = posted[-1] if posted else None
        first_url = ref.extra.get("first_url")

        for i, text in enumerate(thread):
            if i < len(posted):
                continue
            payload: dict = {"text": text}
            if reply_to:
                payload["reply"] = {"in_reply_to_tweet_id": reply_to}
            if i == 0 and media_ids:
                payload["media"] = {"media_ids": media_ids[:1]}
            elif i in (3, 6) and len(media_ids) > 1:
                payload["media"] = {"media_ids": [media_ids.pop(1)]}

            r = await self.request(ctx, "POST", "/2/tweets", json=payload)
            tid = r.json()["data"]["id"]
            posted.append(tid)
            reply_to = tid
            if i == 0:
                first_url = f"https://x.com/i/status/{tid}"
                ref.extra["first_url"] = first_url
            log.info("x: posted %d/%d", i + 1, len(thread))

        return PublishedRef(id=posted[0], url=first_url or f"https://x.com/i/status/{posted[0]}")
