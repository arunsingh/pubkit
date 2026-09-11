# Copyright 2026 The pubkit Authors
# SPDX-License-Identifier: Apache-2.0
"""Dev.to and Hashnode.

These two exist partly for their own sake and partly as proof that the adapter
interface is genuinely platform-agnostic: same IR, same checks, same runner,
about 80 lines each, and native tables and code fences with zero degradation.

Both expect images to already be at public URLs, so they pair with an asset
host (`pubkit.assets`) rather than uploading through an editor.
"""
from __future__ import annotations

import logging

from ..core.adapter import Context, Fingerprint, PublishedRef, RemoteRef
from ..core.capabilities import Capabilities, TagSpec, select_tags
from ..core.ir import Document
from ..render.html import MarkdownRenderer
from .api_base import ApiAdapter

log = logging.getLogger(__name__)


class DevToAdapter(ApiAdapter):
    name = "devto"
    base_url = "https://dev.to/api"
    rate = 0.5
    burst = 3

    capabilities = Capabilities(
        tables=True,
        code_blocks="highlighted",
        inline_html=True,
        animated_gif=True,
        headings=6,
        image_upload="api",
        canonical_url=True,
        tags=TagSpec(max_count=4, max_len=20, strategy="api"),
        drafts=True,
    )

    def __init__(self, asset_urls: dict[str, str] | None = None) -> None:
        super().__init__()
        self.asset_urls = asset_urls or {}
        self.renderer = MarkdownRenderer(image_url_for=self.asset_urls.get)

    def headers(self, ctx: Context) -> dict[str, str]:
        return {"api-key": ctx.tokens.require(self.name), "content-type": "application/json"}

    async def authenticate(self, ctx: Context) -> None:
        await self.request(ctx, "GET", "/users/me")

    async def ensure_draft(self, doc: Document, ctx: Context) -> RemoteRef:
        existing = ctx.options.get("remote_ref")
        if existing:
            return RemoteRef(id=str(existing))
        body = {
            "article": {
                "title": doc.title,
                "published": False,
                "body_markdown": self.renderer.render(doc),
                "tags": [t.replace(" ", "").lower() for t in select_tags(doc.tags, self.capabilities.tags)],
                "canonical_url": doc.canonical_url,
                "description": doc.subtitle or "",
            }
        }
        r = await self.request(ctx, "POST", "/articles", json=body)
        data = r.json()
        return RemoteRef(id=str(data["id"]), url=data.get("url"))

    async def push_content(self, doc, plan, ref, ctx) -> None:
        body = {
            "article": {
                "title": doc.title,
                "body_markdown": self.renderer.render(doc),
                "tags": [t.replace(" ", "").lower() for t in select_tags(doc.tags, self.capabilities.tags)],
                "canonical_url": doc.canonical_url,
                "description": doc.subtitle or "",
            }
        }
        await self.request(ctx, "PUT", f"/articles/{ref.id}", json=body)

    async def verify(self, doc, plan, ref, ctx) -> Fingerprint:
        r = await self.request(ctx, "GET", f"/articles/{ref.id}")
        md = r.json().get("body_markdown", "")
        return Fingerprint(
            words=len(md.split()),
            headings=[ln.lstrip("# ").strip()[:48] for ln in md.splitlines() if ln.startswith("#")],
            images=md.count("!["),
            links=md.count("]("),
            markers=md.count("[[ IMAGE"),
        )

    async def publish(self, doc, ref, ctx) -> PublishedRef:
        self.guard_publish(ctx)
        r = await self.request(ctx, "PUT", f"/articles/{ref.id}", json={"article": {"published": True}})
        data = r.json()
        return PublishedRef(id=str(ref.id), url=data["url"])


class HashnodeAdapter(ApiAdapter):
    name = "hashnode"
    base_url = "https://gql.hashnode.com"
    rate = 1.0
    burst = 5

    capabilities = Capabilities(
        tables=True,
        code_blocks="highlighted",
        inline_html=True,
        animated_gif=True,
        headings=6,
        image_upload="api",
        canonical_url=True,
        tags=TagSpec(max_count=5, max_len=30, strategy="api"),
        drafts=True,
    )

    def __init__(self, publication_id: str = "", asset_urls: dict[str, str] | None = None) -> None:
        super().__init__()
        self.publication_id = publication_id
        self.renderer = MarkdownRenderer(image_url_for=(asset_urls or {}).get)

    def headers(self, ctx: Context) -> dict[str, str]:
        return {"authorization": ctx.tokens.require(self.name), "content-type": "application/json"}

    async def _gql(self, ctx: Context, query: str, variables: dict) -> dict:
        r = await self.request(ctx, "POST", "/", json={"query": query, "variables": variables})
        payload = r.json()
        if payload.get("errors"):
            from ..core.adapter import AdapterError

            raise AdapterError(f"hashnode: {payload['errors'][0].get('message')}")
        return payload["data"]

    async def authenticate(self, ctx: Context) -> None:
        await self._gql(ctx, "query { me { id username } }", {})

    async def ensure_draft(self, doc: Document, ctx: Context) -> RemoteRef:
        existing = ctx.options.get("remote_ref")
        if existing:
            return RemoteRef(id=str(existing))
        q = """
        mutation CreateDraft($input: CreateDraftInput!) {
          createDraft(input: $input) { draft { id slug } }
        }"""
        data = await self._gql(
            ctx,
            q,
            {
                "input": {
                    "title": doc.title,
                    "subtitle": doc.subtitle or "",
                    "contentMarkdown": self.renderer.render(doc),
                    "publicationId": self.publication_id,
                    "tags": [{"name": t, "slug": t.lower().replace(" ", "-")} for t in select_tags(doc.tags, self.capabilities.tags)],
                    "originalArticleURL": doc.canonical_url,
                }
            },
        )
        draft = data["createDraft"]["draft"]
        return RemoteRef(id=draft["id"])

    async def push_content(self, doc, plan, ref, ctx) -> None:
        q = """
        mutation UpdateDraft($input: UpdateDraftInput!) {
          updateDraft(input: $input) { draft { id } }
        }"""
        await self._gql(
            ctx,
            q,
            {"input": {"id": ref.id, "title": doc.title, "contentMarkdown": self.renderer.render(doc)}},
        )

    async def verify(self, doc, plan, ref, ctx) -> Fingerprint:
        data = await self._gql(ctx, "query D($id: ObjectId!) { draft(id: $id) { content { markdown } } }", {"id": ref.id})
        md = data["draft"]["content"]["markdown"]
        return Fingerprint(
            words=len(md.split()),
            headings=[ln.lstrip("# ").strip()[:48] for ln in md.splitlines() if ln.startswith("#")],
            images=md.count("!["),
            links=md.count("]("),
            markers=md.count("[[ IMAGE"),
        )

    async def publish(self, doc, ref, ctx) -> PublishedRef:
        self.guard_publish(ctx)
        q = """
        mutation Publish($input: PublishDraftInput!) {
          publishDraft(input: $input) { post { id url } }
        }"""
        data = await self._gql(ctx, q, {"input": {"draftId": ref.id}})
        post = data["publishDraft"]["post"]
        return PublishedRef(id=post["id"], url=post["url"])
