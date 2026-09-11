# Copyright 2026 The pubkit Authors
# SPDX-License-Identifier: Apache-2.0
"""Materialise the assets a plan promised.

The planner says "8 tables → images" and lists the asset ids. Something has to
actually produce those files, and it has to happen per-platform, because the
same document keeps native tables on Dev.to and needs images on Medium.

This runs between planning and publishing, and is cached on content: a table
whose cells have not changed keeps its rendered file, so a re-run does not
re-render or re-upload it.
"""
from __future__ import annotations

import logging
from pathlib import Path

from ..render.tables import RenderUnavailable, TableStyle, render_table, table_asset_id
from .capabilities import Capabilities
from .ir import Asset, Document, Figure, Table

log = logging.getLogger(__name__)


def materialise(
    doc: Document,
    caps: Capabilities,
    workdir: Path,
    *,
    style: TableStyle | None = None,
) -> Document:
    """Return a copy of `doc` adapted to what `caps` can actually render.

    Tables become Figures on platforms without table support; everything else is
    left alone. The original document is never mutated — the same IR has to
    serve every platform in the same run.
    """
    if caps.tables or not doc.tables:
        return doc

    if caps.image_upload == "none":
        log.warning(
            "%s supports neither tables nor images; %d table(s) will degrade to lists",
            getattr(caps, "name", "platform"),
            len(doc.tables),
        )
        return doc

    out_dir = workdir / "rendered"
    new_blocks: list = []
    new_assets = dict(doc.assets)
    rendered = 0
    reused = 0
    n = 0

    for block in doc.blocks:
        if not isinstance(block, Table):
            new_blocks.append(block)
            continue

        n += 1
        aid = table_asset_id(block, n)
        path = out_dir / f"{aid}.png"

        if not path.exists():
            try:
                render_table(block, path, style)
                rendered += 1
            except RenderUnavailable:
                # Better a readable list than a crash at publish time.
                log.warning("Pillow missing — table %d degrades to a list", n)
                from .ir import ListBlock

                new_blocks.append(
                    ListBlock(
                        ordered=False,
                        items=[
                            " · ".join(f"{h}: {c}" for h, c in zip(block.header, row, strict=False))
                            for row in block.rows
                        ],
                    )
                )
                continue
        else:
            reused += 1

        new_assets[aid] = Asset(
            id=aid, path=path, alt=block.caption or "table", generated_from=f"table[{n}]"
        )
        new_blocks.append(Figure(asset_id=aid, caption=block.caption, alt=block.caption or "table"))

    if rendered or reused:
        log.info("tables → images: %d rendered, %d reused from cache", rendered, reused)

    adapted = doc.model_copy(update={"blocks": new_blocks, "assets": new_assets})
    return adapted
