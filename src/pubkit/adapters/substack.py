# Copyright 2026 The pubkit Authors
# SPDX-License-Identifier: Apache-2.0
"""Substack.

No public write API, so this is a browser adapter — and the point of it being
only ~120 lines is that everything genuinely hard already lives in
`BrowserAdapter`. Substack's editor (ProseMirror) differs from Medium's in its
selectors and in accepting real tables; it does not differ in any of the ways
that caused actual pain.

What is Substack-specific:
  * ProseMirror node selectors instead of Medium's `.graf`
  * native tables, so no table→image degradation
  * publish is a two-step dialog with an email-subscribers choice that must be
    made explicitly rather than left to the default
"""
from __future__ import annotations

import asyncio
import logging
import re

from ..core.adapter import Context, Fingerprint, PublishedRef, RemoteRef
from ..core.browser import BrowserAdapter, EditorSelectors
from ..core.capabilities import Capabilities
from ..core.ir import Document
from ..render.html import EditorHtmlRenderer

log = logging.getLogger(__name__)


class SubstackAdapter(BrowserAdapter):
    name = "substack"
    rate = 0.5
    burst = 2

    capabilities = Capabilities(
        tables=True,
        code_blocks="fenced",
        inline_html=True,
        animated_gif=True,
        headings=4,
        image_upload="browser_paste",
        canonical_url=True,
        tags=None,
        scheduling=True,
        drafts=True,
    )

    selectors = EditorSelectors(
        editable='div[contenteditable="true"].ProseMirror',
        content_roots="div.ProseMirror",
        block="div.ProseMirror > *",
        figure="figure, div[data-attrs*='image']",
        figure_img="img",
        link="a",
        publish_button='button:has-text("Continue")',
        tag_input=None,
        tag_chip=None,
        confirm_publish='button:has-text("Send to everyone now"), button:has-text("Publish now")',
    )

    def __init__(self, publication: str, page=None, upload=None) -> None:
        super().__init__()
        self.publication = publication.rstrip("/")
        self._page = page
        self._upload = upload
        # Substack renders tables natively, so the heading offset is smaller
        # and tables stay tables.
        self.renderer = EditorHtmlRenderer(heading_offset=1, max_heading=4)

    async def authenticate(self, ctx: Context) -> None:
        if ctx.sessions.load(self.name) is None:
            raise RuntimeError(
                "no saved Substack session. Run `pubkit auth login substack` "
                "and sign in yourself in the window that opens."
            )
        await self._page.goto(f"{self.publication}/publish/posts", wait_until="domcontentloaded")
        if "/sign-in" in self._page.url:
            ctx.sessions.forget(self.name)
            raise RuntimeError("saved Substack session expired; run `pubkit auth login substack`")

    async def ensure_draft(self, doc: Document, ctx: Context) -> RemoteRef:
        existing = ctx.options.get("remote_ref")
        if existing:
            await self._page.goto(f"{self.publication}/publish/post/{existing}", wait_until="domcontentloaded")
        else:
            await self._page.goto(f"{self.publication}/publish/post?type=newsletter", wait_until="domcontentloaded")
        await asyncio.sleep(4)
        await self.install_helpers()
        m = re.search(r"/publish/post/(\d+)", self._page.url)
        if not m:
            raise RuntimeError(f"could not determine Substack draft id from {self._page.url}")
        return RemoteRef(id=m.group(1), url=f"{self.publication}/publish/post/{m.group(1)}")

    async def push_content(self, doc, plan, ref, ctx) -> None:
        await self._page.fill('input[placeholder*="Title" i]', doc.title)
        if doc.subtitle:
            await self._page.fill('textarea[placeholder*="subtitle" i], input[placeholder*="subtitle" i]', doc.subtitle)
        await self.install_helpers()
        await self.replace_document(self.renderer.render(doc))

    async def push_media(self, doc, plan, ref, ctx) -> None:
        anchors = self.renderer.caption_anchors(doc)
        if not anchors:
            return
        await self.stage_files([doc.assets[a].path for a, _ in anchors], self._upload)
        used: set[int] = set()
        for i, (asset_id, caption) in enumerate(anchors):
            await self.bucket.acquire()
            await self.insert_image_at(caption, i, used=used)
            log.info("substack: placed %s", asset_id)

    async def verify(self, doc, plan, ref, ctx) -> Fingerprint:
        expected = Fingerprint(words=doc.word_count, headings=[], images=len(doc.figures), links=0)

        async def reload():
            await self._page.goto(f"{self.publication}/publish/post/{ref.id}", wait_until="domcontentloaded")

        return await self.verify_after_reload(expected, reload)

    async def publish(self, doc, ref, ctx) -> PublishedRef:
        self.guard_publish(ctx)
        await self._page.goto(f"{self.publication}/publish/post/{ref.id}", wait_until="domcontentloaded")
        await asyncio.sleep(3)
        await self._page.click(self.selectors.publish_button)
        await asyncio.sleep(2)
        # The email choice is explicit on purpose: silently mailing a few
        # thousand subscribers is not a sensible default for an automation tool.
        if ctx.options.get("email_subscribers", False):
            await self._page.click('button:has-text("Send to everyone now")')
        else:
            await self._page.click('button:has-text("Publish now")')
        await asyncio.sleep(5)
        url = f"{self.publication}/p/{ctx.options.get('slug', ref.id)}"
        return PublishedRef(id=ref.id, url=url)
