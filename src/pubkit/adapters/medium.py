# Copyright 2026 The pubkit Authors
# SPDX-License-Identifier: Apache-2.0
"""Medium.

Medium retired public API tokens for new users, so this is a browser adapter.
It encodes, in order, every trap found while publishing a 15,000-word
illustrated three-part series:

  * no table support at all → tables are pre-rendered to figures by the planner
  * pasted HTML has every image stripped, `data:` and `https:` alike
  * the only working image path is a synthetic File paste into the editor
  * the figure lands above the caret's paragraph, so captions are the anchors
  * DOM edits to links never persist; content must be right before it is sent
  * the topics field will accept one 40-character invalid tag without complaint
  * a delete that looks applied can be gone after a reload

Everything is verified after a reload before anything is published.
"""
from __future__ import annotations

import asyncio
import logging
import re

from ..core.adapter import Context, Fingerprint, PublishedRef, RemoteRef
from ..core.browser import BrowserAdapter, EditorSelectors
from ..core.capabilities import Capabilities, TagSpec, select_tags
from ..core.ir import Document
from ..render.html import EditorHtmlRenderer

log = logging.getLogger(__name__)


class MediumAdapter(BrowserAdapter):
    name = "medium"
    rate = 0.5
    burst = 2

    capabilities = Capabilities(
        tables=False,                  # the single biggest constraint
        code_blocks="plain",
        inline_html=False,
        animated_gif=True,             # GIFs autoplay inline, which is why
                                       # animations survive as GIFs
        headings=4,                    # h3/h4 only inside the body
        max_body_chars=None,
        image_upload="browser_paste",
        canonical_url=True,
        tags=TagSpec(max_count=5, max_len=25, strategy="comma"),
        scheduling=True,
        drafts=True,
    )

    selectors = EditorSelectors(
        editable='.postArticle-content[contenteditable="true"]',
        # ALL section roots. Using only the first is what turned a replace into
        # an append and produced a scrambled, duplicated draft.
        content_roots=".section-inner",
        block=".graf",
        figure="figure",
        figure_img="figure img",
        link="a",
        publish_button='button[data-action="show-publish-flow"], [data-testid="publishButton"]',
        tag_input='input[placeholder*="topic" i], input[data-testid="topicInput"]',
        tag_chip='[data-testid="topicChip"], .js-tagToken',
        confirm_publish='button:has-text("Publish now")',
    )

    login_url = "https://medium.com/m/signin"
    new_story_url = "https://medium.com/new-story"
    marker_regex = r"\\[\\[\\s*IMAGE"

    def __init__(self, page=None, upload=None) -> None:
        super().__init__()
        self._page = page
        self._upload = upload
        self.renderer = EditorHtmlRenderer(heading_offset=2, max_heading=4)

    # ------------------------------------------------------------------ auth
    async def authenticate(self, ctx: Context) -> None:
        """Session-based. pubkit never sees a password.

        `pubkit auth login medium` opens a visible window, the user signs in,
        and the resulting storage_state is what gets persisted.
        """
        state = ctx.sessions.load(self.name)
        if state is None:
            raise RuntimeError(
                "no saved Medium session. Run `pubkit auth login medium` — "
                "a browser window opens, you sign in yourself, pubkit stores "
                "only the session cookie."
            )
        await self._page.goto("https://medium.com/me/stories/drafts", wait_until="domcontentloaded")
        if "signin" in self._page.url:
            ctx.sessions.forget(self.name)
            raise RuntimeError("saved Medium session has expired; run `pubkit auth login medium`")

    # ----------------------------------------------------------------- draft
    async def ensure_draft(self, doc: Document, ctx: Context) -> RemoteRef:
        """Reuse the recorded draft if there is one; never create a duplicate."""
        existing = ctx.options.get("remote_ref")
        if existing:
            await self._page.goto(f"https://medium.com/p/{existing}/edit", wait_until="domcontentloaded")
            await asyncio.sleep(3)
            await self.install_helpers()
            return RemoteRef(id=existing, url=f"https://medium.com/p/{existing}")

        await self._page.goto(self.new_story_url, wait_until="domcontentloaded")
        await asyncio.sleep(4)
        await self.install_helpers()
        m = re.search(r"/p/([0-9a-f]{8,})/edit", self._page.url)
        if not m:
            # A brand-new story gets its id on first save; nudge the editor.
            await self._page.click(self.selectors.editable)
            await self._page.keyboard.type(doc.title[:8])
            await asyncio.sleep(3)
            m = re.search(r"/p/([0-9a-f]{8,})/edit", self._page.url)
        if not m:
            raise RuntimeError(f"could not determine draft id from {self._page.url}")
        return RemoteRef(id=m.group(1), url=f"https://medium.com/p/{m.group(1)}")

    # --------------------------------------------------------------- content
    async def push_content(self, doc, plan, ref, ctx) -> None:
        """One paste for the whole document, with the link targets already
        correct.

        Links must be right *before* transmission: setting an href in the DOM
        afterwards looks like it worked and is gone on the next reload, because
        the editor syncs from its own model (failure B4).
        """
        html = self.renderer.render(doc)
        if "URL-PART" in html or "${" in html:
            raise RuntimeError(
                "unresolved cross-reference placeholder in rendered HTML — "
                "run phase 1 of the two-phase publish first"
            )
        await self.install_helpers()
        await self.replace_document(html)

    # ----------------------------------------------------------------- media
    async def push_media(self, doc, plan, ref, ctx) -> None:
        anchors = self.renderer.caption_anchors(doc)
        if not anchors:
            return
        paths = [doc.assets[aid].path for aid, _ in anchors]
        await self.stage_files(paths, self._upload)

        used: set[int] = set()
        for i, (asset_id, caption) in enumerate(anchors):
            await self.bucket.acquire()
            await self.insert_image_at(caption, i, used=used)
            log.info("medium: placed %s above %r", asset_id, caption[:48])

    # ---------------------------------------------------------------- verify
    async def verify(self, doc, plan, ref, ctx) -> Fingerprint:
        expected = Fingerprint(
            words=doc.word_count,
            headings=[],
            images=len(doc.figures),
            links=0,
        )

        async def reload():
            await self._page.goto(f"https://medium.com/p/{ref.id}/edit", wait_until="domcontentloaded")

        return await self.verify_after_reload(expected, reload)

    # --------------------------------------------------------------- publish
    async def publish(self, doc, ref, ctx) -> PublishedRef:
        self.guard_publish(ctx)
        await self._page.goto(f"https://medium.com/p/{ref.id}/edit", wait_until="domcontentloaded")
        await asyncio.sleep(3)
        await self._page.click(self.selectors.publish_button)
        await asyncio.sleep(3)

        tags = select_tags(doc.tags, self.capabilities.tags)
        await self.apply_tags(tags, self.capabilities.tags.strategy)

        await self._page.click(self.selectors.confirm_publish)
        await self._page.wait_for_url(re.compile(r"medium\.com/(@|p/)"), timeout=60_000)
        await asyncio.sleep(2)
        url = self._page.url.split("?")[0]
        log.info("medium: published %s", url)
        return PublishedRef(id=ref.id, url=url)
