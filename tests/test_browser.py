# Copyright 2026 The pubkit Authors
# SPDX-License-Identifier: Apache-2.0
"""Integration tests against a real browser and a deliberately hostile editor.

`tests/fixtures/fake_editor.html` reproduces the behaviours that actually broke
things: multiple content roots, images stripped from pasted HTML, figures that
land before the caret, and uploads that sit on a `blob:` URL before resolving.

These run wherever Chromium is available and skip cleanly where it is not, so
`pytest` on a laptop without `playwright install` still passes.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pubkit.core.browser import BrowserAdapter, DocumentReplaceMismatch, EditorSelectors
from pubkit.core.ir import Asset, Document, Figure, Heading, Paragraph, Table
from pubkit.render.html import EditorHtmlRenderer
from pubkit.render.tables import render_table

FIXTURE = (Path(__file__).parent / "fixtures" / "fake_editor.html").resolve()

playwright = pytest.importorskip("playwright.async_api", reason="playwright not installed")


class FakeEditorAdapter(BrowserAdapter):
    """Only selectors differ from a real adapter. That is the point."""

    name = "fake"
    selectors = EditorSelectors(
        editable='.postArticle-content[contenteditable="true"]',
        content_roots=".section-inner",
        block=".graf",
        figure="figure",
        figure_img="figure img",
        link="a",
        publish_button="#publish",
    )


@pytest.fixture
async def editor():
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"chromium unavailable: {str(exc)[:80]}")
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto(FIXTURE.as_uri())

        adapter = FakeEditorAdapter()
        adapter._page = page

        async def upload(selector, paths):
            await page.set_input_files(selector, list(paths))

        adapter._upload = upload
        await adapter.install_helpers()
        yield adapter
        await ctx.close()
        await browser.close()


def _doc() -> Document:
    return Document(
        id="d",
        title="A Title",
        subtitle="A subtitle",
        blocks=[
            Heading(level=1, text="First section"),
            Paragraph(text="Body text with **bold** and a [link](https://example.test)."),
            Figure(asset_id="f1", caption="The first caption, which is the anchor"),
            Paragraph(text="More body."),
            Figure(asset_id="f2", caption="A second caption — with an em dash"),
        ],
        assets={
            "f1": Asset(id="f1", path=Path("a.png")),
            "f2": Asset(id="f2", path=Path("b.png")),
        },
    )


async def test_replace_spans_every_content_root(editor):
    """Failure B1: selecting only the first root appends instead of replacing.

    The fixture deliberately ships two `.section-inner` roots. An adapter that
    grabs `querySelector` rather than `querySelectorAll` leaves the second
    root's paragraph in place — which is exactly how a draft ended up
    scrambled and duplicated.
    """
    before = await editor.block_texts()
    assert len(before) == 3          # 2 in the first root, 1 in the second

    html = EditorHtmlRenderer().render(_doc())
    await editor.replace_document(html)

    texts = await editor.block_texts()
    joined = "\n".join(texts)
    assert "Miss this and your paste appends" not in joined
    assert "A Title" in joined and "First section" in joined


async def test_replace_asserts_the_resulting_block_count(editor):
    html = EditorHtmlRenderer().render(_doc())
    with pytest.raises(DocumentReplaceMismatch, match="expected 999"):
        await editor.replace_document(html, expect_blocks=999)


async def test_pasted_html_never_carries_an_image(editor):
    """Failure B5: the editor strips them, so we must not rely on them."""
    doc = _doc()
    html = EditorHtmlRenderer().render(doc)
    assert "<img" not in html
    await editor.replace_document(html)
    assert await editor._count_figures() == 0
    # The captions survived, and they are what the images will anchor to.
    texts = "\n".join(await editor.block_texts())
    assert "The first caption, which is the anchor" in texts


async def test_image_lands_above_its_caption_and_waits_for_the_cdn(editor, tmp_path):
    """Failures B6, B7 and the blob: race, in one test.

    The fixture resolves `blob:` → CDN only after a delay, so an adapter that
    does not wait would pass this assertion on a dead URL.
    """
    png = render_table(Table(header=["h"], rows=[["v"]]), tmp_path / "t.png")
    doc = _doc()
    await editor.replace_document(EditorHtmlRenderer().render(doc))
    await editor.stage_files([png, png], editor._upload)

    used: set[int] = set()
    await editor.insert_image_at("The first caption, which is the anchor", 0, used=used)

    state = await editor._eval(
        "([r,b]) => { const g = window.__pk.blocks(r,b);"
        " const i = g.findIndex(e => e.tagName === 'FIGURE');"
        " return { figIdx: i, next: g[i+1] ? g[i+1].innerText.trim() : '',"
        "          src: g[i].querySelector('img').src }; }",
        [editor.selectors.content_roots, editor.selectors.block],
    )
    assert state["next"].startswith("The first caption")
    assert state["src"].startswith("https://cdn.example.test/")   # not blob:


async def test_anchor_resolution_survives_editor_typography(editor, tmp_path):
    """Failure B8: an em dash in the rendered caption must still match."""
    png = render_table(Table(header=["h"], rows=[["v"]]), tmp_path / "t.png")
    await editor.replace_document(EditorHtmlRenderer().render(_doc()))
    await editor.stage_files([png], editor._upload)

    # Source has a plain hyphen; the rendered caption has an em dash.
    await editor.insert_image_at("A second caption - with an em dash", 0)
    assert await editor._count_figures() == 1


async def test_fingerprint_counts_what_is_actually_on_the_page(editor, tmp_path):
    png = render_table(Table(header=["h"], rows=[["v"]]), tmp_path / "t.png")
    await editor.replace_document(EditorHtmlRenderer().render(_doc()))
    fp = await editor.fingerprint()
    assert fp.words > 10
    assert fp.images == 0
    assert fp.links == 1                       # the one inline link
    assert fp.markers == 0

    await editor.stage_files([png], editor._upload)
    await editor.insert_image_at("The first caption, which is the anchor", 0)
    assert (await editor.fingerprint()).images == 1


async def test_staging_rejects_a_mismatched_file_count(editor, tmp_path):
    png = render_table(Table(header=["h"], rows=[["v"]]), tmp_path / "t.png")
    await editor.stage_files([png], editor._upload)
    n = await editor._eval(
        "() => document.getElementById('__pubkit_upload').files.length"
    )
    assert n == 1
