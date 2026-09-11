# Copyright 2026 The pubkit Authors
# SPDX-License-Identifier: Apache-2.0
"""Markdown + front-matter → Post IR."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from .ir import (
    Asset,
    Budget,
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
    Series,
    SeriesRef,
    Table,
)


class FrontMatterError(ValueError):
    """Bad YAML front-matter, reported in terms an author can act on."""


FM_RE = re.compile(r"^---\n(.*?)\n---\n", re.S)
FIG_RE = re.compile(r"^!\[(?P<alt>[^\]]*)\]\((?P<src>[^)\s]+)\)(?:\s*\"(?P<cap>[^\"]*)\")?\s*$")
CALLOUT_RE = re.compile(r"^>\s*\[!(?P<style>note|warning|tip|key)\]\s*(?P<title>.*)$", re.I)


def _table(lines: list[str], caption: str | None) -> Table:
    cells = [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in lines]
    header, sep, *rows = cells
    align = []
    for s in sep:
        align.append("c" if s.startswith(":") and s.endswith(":") else "r" if s.endswith(":") else "l")
    return Table(header=header, rows=rows, align=align, caption=caption)


def parse_markdown(text: str, *, base: Path) -> tuple[dict, list, dict[str, Asset]]:
    meta: dict = {}
    m = FM_RE.match(text)
    if m:
        try:
            meta = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError as exc:
            # Titles routinely contain a colon ("Part 1: The Hardware"), which
            # is invalid unquoted YAML. Say so plainly instead of surfacing a
            # parser traceback at the author.
            raise FrontMatterError(
                f"invalid front-matter: {exc}\n"
                "Tip: quote any value containing a colon, e.g.\n"
                '  title: "Inside AI Infrastructure, Part 1: The Hardware"'
            ) from exc
        if not isinstance(meta, dict):
            raise FrontMatterError("front-matter must be a mapping of keys to values")
        text = text[m.end() :]

    blocks: list = []
    assets: dict[str, Asset] = {}
    lines = text.split("\n")
    i = 0
    fig_n = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # fenced code
        if stripped.startswith("```"):
            lang = stripped[3:].strip()
            i += 1
            buf = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            blocks.append(Code(language=lang, text="\n".join(buf)))
            continue

        # horizontal rule
        if re.fullmatch(r"(\*\s*){3,}|(-\s*){3,}|(_\s*){3,}", stripped):
            blocks.append(Rule())
            i += 1
            continue

        # heading
        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            blocks.append(Heading(level=level, text=stripped[level:].strip()))
            i += 1
            continue

        # table
        if stripped.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[\s:\-|]+\|$", lines[i + 1].strip()):
            buf = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                buf.append(lines[i])
                i += 1
            cap = None
            if i < len(lines) and lines[i].strip().startswith("*") and lines[i].strip().endswith("*"):
                cap = lines[i].strip().strip("*")
                i += 1
            blocks.append(_table(buf, cap))
            continue

        # figure
        fm = FIG_RE.match(stripped)
        if fm:
            fig_n += 1
            aid = f"fig-{fig_n:02d}"
            src = fm.group("src")
            assets[aid] = Asset(id=aid, path=(base / src).resolve(), alt=fm.group("alt") or "")
            cap = fm.group("cap")
            if cap is None and i + 1 < len(lines):
                nxt = lines[i + 1].strip()
                if nxt.startswith("*") and nxt.endswith("*") and len(nxt) > 2:
                    cap = nxt.strip("*")
                    i += 1
            blocks.append(Figure(asset_id=aid, caption=cap, alt=fm.group("alt") or ""))
            i += 1
            continue

        # callout
        cm = CALLOUT_RE.match(stripped)
        if cm:
            i += 1
            buf = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip().lstrip(">").strip())
                i += 1
            blocks.append(
                Callout(style=cm.group("style").lower(), title=cm.group("title") or None, text=" ".join(buf))
            )
            continue

        # quote
        if stripped.startswith(">"):
            buf = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip().lstrip(">").strip())
                i += 1
            blocks.append(Quote(text=" ".join(b for b in buf if b)))
            continue

        # list
        if re.match(r"^([-*+]|\d+\.)\s+", stripped):
            ordered = bool(re.match(r"^\d+\.", stripped))
            items = []
            while i < len(lines) and re.match(r"^([-*+]|\d+\.)\s+", lines[i].strip()):
                items.append(re.sub(r"^([-*+]|\d+\.)\s+", "", lines[i].strip()))
                i += 1
            blocks.append(ListBlock(ordered=ordered, items=items))
            continue

        # embed on its own line
        if re.fullmatch(r"https?://\S+", stripped):
            kind = (
                "youtube" if "youtu" in stripped
                else "gist" if "gist.github" in stripped
                else "tweet" if ("twitter.com" in stripped or "x.com" in stripped)
                else "generic"
            )
            blocks.append(Embed(url=stripped, kind=kind))
            i += 1
            continue

        # paragraph
        buf = []
        while i < len(lines) and lines[i].strip() and not lines[i].strip().startswith(("#", ">", "|", "```")):
            if FIG_RE.match(lines[i].strip()):
                break
            buf.append(lines[i].strip())
            i += 1
        if buf:
            blocks.append(Paragraph(text=" ".join(buf)))

    return meta, blocks, assets


def load_document(path: Path) -> Document:
    meta, blocks, assets = parse_markdown(path.read_text(encoding="utf-8"), base=path.parent)
    series = None
    if "series" in meta:
        s = meta["series"]
        series = SeriesRef(id=s["id"], index=int(s["index"]), of=int(s["of"]))
    budget = None
    if "budget" in meta:
        b = meta["budget"]
        budget = Budget(words=b.get("words"), tolerance=float(b.get("tolerance", 0.15)))

    doc = Document(
        id=meta.get("id") or path.stem,
        title=meta.get("title") or "Untitled",
        subtitle=meta.get("subtitle"),
        tags=list(meta.get("tags") or []),
        canonical_url=meta.get("canonical_url"),
        blocks=blocks,
        assets=assets,
        series=series,
        budget=budget,
        platform_overrides=meta.get("platforms") or {},
    )
    number_sections(doc)
    return doc


def number_sections(doc: Document, start: int = 1) -> None:
    """Generate section numbers.

    Hand-numbered sections are how "Part 1" ended up meaning both a series part
    and a section heading in the same document, breaking seven cross-references
    when one article became three (failure C2). Numbering is generated here and
    nowhere else.
    """
    major = start - 1
    minor = 0
    for b in doc.blocks:
        if isinstance(b, Heading):
            if b.level == 1:
                major += 1
                minor = 0
                b.number = str(major)
            elif b.level == 2 and major > 0:
                minor += 1
                b.number = f"{major}.{minor}"
            else:
                b.number = None


def load_series(paths: list[Path]) -> Series:
    docs = [load_document(p) for p in paths]
    docs.sort(key=lambda d: d.series.index if d.series else 0)
    # Continue section numbering across the series so chapter 4 follows 3.
    offset = 1
    for d in docs:
        number_sections(d, start=offset)
        offset += sum(1 for b in d.blocks if isinstance(b, Heading) and b.level == 1)
    sid = docs[0].series.id if docs[0].series else "series"
    return Series(id=sid, title=docs[0].title.split(",")[0], documents=docs)


def resolve_series_links(series: Series) -> None:
    """Phase 2 of the two-phase publish: fill in `${series.partN.url}`.

    Until every draft exists, a cross-link has nothing to point at. Publishing
    with the placeholder still in place is how `URL-PART-2` nearly reached a
    live post (failure C3).
    """
    urls = {
        d.series.index: d.series.sibling_urls.get(d.series.index) or ""
        for d in series.documents
        if d.series
    }
    for d in series.documents:
        if d.series:
            urls.update(d.series.sibling_urls)
    for d in series.documents:
        for b in d.blocks:
            text = getattr(b, "text", None)
            if not text:
                continue
            for idx, url in urls.items():
                if url:
                    text = text.replace(f"${{series.part{idx}.url}}", url)
            b.text = text
