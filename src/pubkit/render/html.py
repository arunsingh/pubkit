# Copyright 2026 The pubkit Authors
# SPDX-License-Identifier: Apache-2.0
"""Renderers: IR → what a platform will actually accept."""
from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass

from ..core.ir import (
    Callout,
    Code,
    Document,
    Embed,
    Figure,
    Heading,
    ListBlock,
    Paragraph,
    Quote,
    Rule,
    Table,
)

_INLINE = [
    (re.compile(r"\*\*(.+?)\*\*", re.S), r"<strong>\1</strong>"),
    (re.compile(r"(?<!\*)\*([^*]+?)\*(?!\*)"), r"<em>\1</em>"),
    (re.compile(r"`([^`]+?)`"), r"<code>\1</code>"),
    (re.compile(r"\[([^\]]+?)\]\(([^)]+?)\)"), r'<a href="\2">\1</a>'),
]


def inline(text: str) -> str:
    out = _html.escape(text, quote=False)
    for pattern, repl in _INLINE:
        out = pattern.sub(repl, out)
    return out


@dataclass
class EditorHtmlRenderer:
    """HTML shaped for a contenteditable editor, not for the web.

    Two deliberate constraints:

    * **No images.** Figures render as a caption paragraph only. The image
      arrives later through the editor's own upload path, because both `data:`
      URIs and remote URLs are stripped from pasted HTML (failure B5).
      The caption doubles as the insertion anchor.

    * **Tables are pre-rendered to images upstream.** By the time a table gets
      here it is already a Figure, or the planner decided the platform supports
      tables and a different renderer is in use.

    Heading levels are remapped because editors reserve h1/h2 for title and
    subtitle; article headings start at h3.
    """

    heading_offset: int = 2
    max_heading: int = 4

    def render(self, doc: Document) -> str:
        parts: list[str] = [f"<h3>{inline(doc.title)}</h3>"]
        if doc.subtitle:
            parts.append(f"<h4>{inline(doc.subtitle)}</h4>")
        for block in doc.blocks:
            parts.append(self.block(block, doc))
        return "".join(p for p in parts if p)

    def block(self, b, doc: Document) -> str:
        if isinstance(b, Heading):
            lvl = min(self.max_heading, b.level + self.heading_offset)
            prefix = f"{b.number}. " if b.number else ""
            return f"<h{lvl}>{inline(prefix + b.text)}</h{lvl}>"
        if isinstance(b, Paragraph):
            return f"<p>{inline(b.text)}</p>"
        if isinstance(b, Code):
            return f"<pre><code>{_html.escape(b.text)}</code></pre>"
        if isinstance(b, Quote):
            attr = f"<br><em>— {inline(b.attribution)}</em>" if b.attribution else ""
            return f"<blockquote>{inline(b.text)}{attr}</blockquote>"
        if isinstance(b, ListBlock):
            tag = "ol" if b.ordered else "ul"
            items = "".join(f"<li>{inline(i)}</li>" for i in b.items)
            return f"<{tag}>{items}</{tag}>"
        if isinstance(b, Callout):
            title = f"<strong>{inline(b.title)}</strong><br>" if b.title else ""
            return f"<blockquote>{title}{inline(b.text)}</blockquote>"
        if isinstance(b, Rule):
            return "<hr>"
        if isinstance(b, Embed):
            return f'<p><a href="{_html.escape(b.url, quote=True)}">{_html.escape(b.url)}</a></p>'
        if isinstance(b, Figure):
            # Caption only. It is both the visible caption and the anchor the
            # image will be inserted above.
            cap = b.caption or (doc.assets[b.asset_id].alt if b.asset_id in doc.assets else "")
            return f"<p><em>{inline(cap)}</em></p>" if cap else ""
        if isinstance(b, Table):
            head = "".join(f"<th>{inline(h)}</th>" for h in b.header)
            rows = "".join(
                "<tr>" + "".join(f"<td>{inline(c)}</td>" for c in row) + "</tr>" for row in b.rows
            )
            cap = f"<caption>{inline(b.caption)}</caption>" if b.caption else ""
            return f"<table>{cap}<thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>"
        return ""

    def caption_anchors(self, doc: Document) -> list[tuple[str, str]]:
        """(asset_id, caption) in document order — the insertion plan."""
        out = []
        for b in doc.blocks:
            if isinstance(b, Figure):
                cap = b.caption or (doc.assets[b.asset_id].alt if b.asset_id in doc.assets else "")
                if cap:
                    out.append((b.asset_id, cap))
        return out


@dataclass
class MarkdownRenderer:
    """Plain Markdown for API platforms (Dev.to, Hashnode, Ghost, static sites)."""

    image_url_for: object = None  # Callable[[str], str] | None

    def render(self, doc: Document) -> str:
        lines: list[str] = []
        for b in doc.blocks:
            lines.append(self.block(b, doc))
        return "\n\n".join(x for x in lines if x).strip() + "\n"

    def block(self, b, doc: Document) -> str:
        if isinstance(b, Heading):
            prefix = f"{b.number}. " if b.number else ""
            return "#" * min(6, b.level + 1) + " " + prefix + b.text
        if isinstance(b, Paragraph):
            return b.text
        if isinstance(b, Code):
            return f"```{b.language}\n{b.text}\n```"
        if isinstance(b, Quote):
            body = "\n".join("> " + ln for ln in b.text.splitlines())
            return body + (f"\n>\n> — {b.attribution}" if b.attribution else "")
        if isinstance(b, ListBlock):
            return "\n".join(
                (f"{i+1}. " if b.ordered else "- ") + item for i, item in enumerate(b.items)
            )
        if isinstance(b, Callout):
            title = f"**{b.title}**\n>\n" if b.title else ""
            return "> " + title.replace("\n", "\n> ") + b.text
        if isinstance(b, Rule):
            return "---"
        if isinstance(b, Embed):
            return b.url
        if isinstance(b, Figure):
            asset = doc.assets.get(b.asset_id)
            alt = b.alt or (asset.alt if asset else "")
            url = self.image_url_for(b.asset_id) if self.image_url_for else (str(asset.path) if asset else "")
            cap = f"\n*{b.caption}*" if b.caption else ""
            return f"![{alt}]({url}){cap}"
        if isinstance(b, Table):
            align = b.align or ["l"] * len(b.header)
            sep = {"l": ":---", "c": ":---:", "r": "---:"}
            head = "| " + " | ".join(b.header) + " |"
            rule = "| " + " | ".join(sep.get(a, ":---") for a in align) + " |"
            rows = "\n".join("| " + " | ".join(r) + " |" for r in b.rows)
            cap = f"\n*{b.caption}*" if b.caption else ""
            return f"{head}\n{rule}\n{rows}{cap}"
        return ""


MD_STRIP = [
    (re.compile(r"\*\*(.+?)\*\*", re.S), r"\1"),
    (re.compile(r"(?<!\*)\*([^*]+?)\*(?!\*)"), r"\1"),
    (re.compile(r"`([^`]+?)`"), r"\1"),
    (re.compile(r"\[([^\]]+?)\]\(([^)]+?)\)"), r"\1"),
    (re.compile(r"<!--.*?-->", re.S), ""),
]


def plain(text: str) -> str:
    """Strip inline markdown.

    Platforms that take plain text render `**bold**` literally, which reads as
    a formatting bug to every reader who sees it.
    """
    for pattern, repl in MD_STRIP:
        text = pattern.sub(repl, text)
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class ThreadRenderer:
    """Split a document into a numbered thread for X/Bluesky/Mastodon."""

    limit: int = 280
    reserve: int = 12  # room for " (3/11)"

    def render(self, doc: Document) -> list[str]:
        chunks: list[str] = []
        budget = self.limit - self.reserve
        buf = ""
        for b in doc.blocks:
            if isinstance(b, (Heading, Rule, Figure, Table)):
                if buf:
                    chunks.append(buf.strip())
                    buf = ""
                if isinstance(b, Heading):
                    buf = b.text.strip() + "\n\n"
                continue
            text = getattr(b, "text", "") or (
                "\n".join(b.items) if isinstance(b, ListBlock) else ""
            )
            for sentence in re.split(r"(?<=[.!?])\s+", plain(text)):
                if not sentence:
                    continue
                if len(buf) + len(sentence) + 1 > budget:
                    chunks.append(buf.strip())
                    buf = ""
                buf += sentence + " "
        if buf.strip():
            chunks.append(buf.strip())

        total = len(chunks)
        return [f"{c} ({i+1}/{total})" if total > 1 else c for i, c in enumerate(chunks)]
