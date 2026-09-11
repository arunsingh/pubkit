# Copyright 2026 The pubkit Authors
# SPDX-License-Identifier: Apache-2.0
"""Capability declarations and the planner that negotiates against them.

The planner's job is to turn "here is my content" plus "here is what this
platform can do" into an explicit, printable list of *degradations*. Surprises
at publish time are the enemy; `pubkit plan` exists so there are none.
"""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from .ir import Document, Table


class TagSpec(BaseModel):
    model_config = {"extra": "forbid"}

    max_count: int = 5
    max_len: int = 25
    #: How the platform's tag widget actually commits a tag (failure B9).
    strategy: Literal["comma", "enter", "native_setter", "api"] = "api"


class Capabilities(BaseModel):
    model_config = {"extra": "forbid"}

    tables: bool = True
    code_blocks: Literal["fenced", "highlighted", "plain", "none"] = "fenced"
    inline_html: bool = False
    animated_gif: bool = True
    headings: int = 6
    max_body_chars: int | None = None
    image_upload: Literal["api", "browser_paste", "none"] = "api"
    canonical_url: bool = False
    tags: TagSpec | None = None
    scheduling: bool = False
    threads: bool = False
    #: Whether drafts can be created and revisited, or publishing is one-shot.
    drafts: bool = True


class DegradationKind(str, Enum):
    TABLE_TO_IMAGE = "table_to_image"
    TABLE_TO_LIST = "table_to_list"
    HTML_STRIPPED = "html_stripped"
    CODE_FLATTENED = "code_flattened"
    HEADINGS_CLAMPED = "headings_clamped"
    SPLIT_INTO_THREAD = "split_into_thread"
    GIF_TO_STILL = "gif_to_still"
    TAGS_TRIMMED = "tags_trimmed"
    IMAGES_VIA_BROWSER = "images_via_browser"
    BODY_TRUNCATED = "body_truncated"


class Degradation(BaseModel):
    model_config = {"extra": "forbid"}

    kind: DegradationKind
    detail: str
    count: int = 1
    #: True when the degradation loses information the author may care about.
    lossy: bool = False


class PublishPlan(BaseModel):
    model_config = {"extra": "forbid"}

    document_id: str
    content_id: str
    platform: str
    degradations: list[Degradation] = Field(default_factory=list)
    #: Assets the renderer must generate before publishing (e.g. table PNGs).
    generated_assets: list[str] = Field(default_factory=list)
    estimated_body_chars: int = 0
    blocking: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blocking

    def human(self) -> str:
        lines = [f"{self.platform}: {self.document_id} ({self.content_id[:12]})"]
        if not self.degradations:
            lines.append("    no degradation — native fidelity")
        for d in self.degradations:
            mark = "!" if d.lossy else "·"
            lines.append(f"  {mark} {d.kind.value:<20} {d.detail}")
        for b in self.blocking:
            lines.append(f"  ✗ BLOCKING {b}")
        return "\n".join(lines)


def plain_text_length(doc: Document) -> int:
    """Characters a reader actually sees."""
    total = len(doc.title) + len(doc.subtitle or "")
    for b in doc.blocks:
        text = getattr(b, "text", None)
        if text:
            total += len(text)
        items = getattr(b, "items", None)
        if items:
            total += sum(len(i) for i in items)
        if isinstance(b, Table):
            total += sum(len(c) for c in b.header)
            total += sum(len(c) for row in b.rows for c in row)
    return total


def plan(doc: Document, platform: str, caps: Capabilities, adapter: object | None = None) -> PublishPlan:
    """Intersect a document with a platform's capabilities.

    Static capabilities cannot express everything: X in promo mode never
    overflows 280 chars because it does not serialise the body at all. An
    adapter may therefore refine its own plan — the last word belongs to the
    code that will actually do the work.
    """
    p = PublishPlan(document_id=doc.id, content_id=doc.content_id, platform=platform)

    # --- tables -----------------------------------------------------------
    tables = doc.tables
    if tables and not caps.tables:
        if caps.image_upload != "none":
            p.degradations.append(
                Degradation(
                    kind=DegradationKind.TABLE_TO_IMAGE,
                    detail=f"{len(tables)} table(s) rendered as images — platform has no table support",
                    count=len(tables),
                )
            )
            p.generated_assets.extend(f"table-{i:02d}" for i in range(len(tables)))
        else:
            p.degradations.append(
                Degradation(
                    kind=DegradationKind.TABLE_TO_LIST,
                    detail=f"{len(tables)} table(s) flattened to lists",
                    count=len(tables),
                    lossy=True,
                )
            )

    # --- figures ----------------------------------------------------------
    figures = doc.figures
    if figures:
        if caps.image_upload == "none":
            p.blocking.append(f"{len(figures)} figure(s) but platform accepts no images")
        elif caps.image_upload == "browser_paste":
            p.degradations.append(
                Degradation(
                    kind=DegradationKind.IMAGES_VIA_BROWSER,
                    detail=(
                        f"{len(figures)} image(s) uploaded through the editor's own paste path "
                        "— remote URLs and data: URIs are stripped by this platform"
                    ),
                    count=len(figures),
                )
            )
        if not caps.animated_gif:
            gifs = [f for f in figures if doc.assets[f.asset_id].animated]
            if gifs:
                p.degradations.append(
                    Degradation(
                        kind=DegradationKind.GIF_TO_STILL,
                        detail=f"{len(gifs)} animation(s) reduced to a still frame",
                        count=len(gifs),
                        lossy=True,
                    )
                )

    # --- code -------------------------------------------------------------
    if caps.code_blocks in ("plain", "none"):
        n = sum(1 for b in doc.blocks if getattr(b, "type", None) == "code")
        if n:
            p.degradations.append(
                Degradation(
                    kind=DegradationKind.CODE_FLATTENED,
                    detail=f"{n} code block(s) lose syntax highlighting",
                    count=n,
                )
            )

    # --- headings ---------------------------------------------------------
    deep = [b for b in doc.blocks if getattr(b, "type", None) == "heading" and b.level > caps.headings]
    if deep:
        p.degradations.append(
            Degradation(
                kind=DegradationKind.HEADINGS_CLAMPED,
                detail=f"{len(deep)} heading(s) clamped to h{caps.headings}",
                count=len(deep),
            )
        )

    # --- length -----------------------------------------------------------
    # Plain-text length, not the canonical JSON: JSON includes keys, quoting
    # and escapes, which inflated a 3,300-word article into an estimated
    # 94-tweet thread. An estimate that is wrong by 4x is worse than none.
    approx = plain_text_length(doc)
    p.estimated_body_chars = approx
    if caps.max_body_chars and approx > caps.max_body_chars:
        if caps.threads:
            parts = -(-approx // caps.max_body_chars)
            p.degradations.append(
                Degradation(
                    kind=DegradationKind.SPLIT_INTO_THREAD,
                    detail=f"body split into ~{parts} posts",
                    count=parts,
                )
            )
        else:
            p.blocking.append(
                f"body is {approx} chars, platform limit is {caps.max_body_chars} "
                "and it has no thread support"
            )

    # --- tags -------------------------------------------------------------
    if doc.tags:
        if caps.tags is None:
            p.degradations.append(
                Degradation(
                    kind=DegradationKind.TAGS_TRIMMED,
                    detail=f"{len(doc.tags)} tag(s) dropped — platform has no tags",
                    count=len(doc.tags),
                )
            )
        else:
            over_len = [t for t in doc.tags if len(t) > caps.tags.max_len]
            over_count = max(0, len(doc.tags) - caps.tags.max_count)
            if over_len or over_count:
                p.degradations.append(
                    Degradation(
                        kind=DegradationKind.TAGS_TRIMMED,
                        detail=(
                            f"keeping {min(len(doc.tags), caps.tags.max_count)} of {len(doc.tags)}"
                            + (f"; {len(over_len)} exceed {caps.tags.max_len} chars" if over_len else "")
                        ),
                        count=len(doc.tags),
                    )
                )

    if adapter is not None and hasattr(adapter, "refine_plan"):
        p = adapter.refine_plan(doc, p)
    return p


def select_tags(tags: list[str], spec: TagSpec | None) -> list[str]:
    """Trim a tag list to what a platform will actually accept.

    Silently sending an over-long tag is how one publish attempt ended up with a
    single 40-character invalid tag instead of five good ones (failure B9).
    """
    if spec is None:
        return []
    return [t for t in tags if len(t) <= spec.max_len][: spec.max_count]
