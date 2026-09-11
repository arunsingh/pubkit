# Copyright 2026 The pubkit Authors
# SPDX-License-Identifier: Apache-2.0
"""Tests named after the failures they prevent."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from pubkit.core.anchors import find_anchor, normalise
from pubkit.core.capabilities import Capabilities, DegradationKind, TagSpec, plan, select_tags
from pubkit.core.checks import Severity, run_checks
from pubkit.core.ir import Asset, Budget, Document, Figure, Heading, Paragraph, SeriesRef, Table
from pubkit.core.loader import load_document, number_sections
from pubkit.core.state import StateStore, Status, Step
from pubkit.core.transport import (
    ChunkedTransport,
    TransportCorruption,
    decode_payload,
    encode_payload,
    rolling_hash,
)
from pubkit.render.html import EditorHtmlRenderer, MarkdownRenderer, ThreadRenderer


# ---------------------------------------------------------------- IR basics
def _doc(**kw) -> Document:
    base = {
        "id": "d1",
        "title": "T",
        "blocks": [Paragraph(text="hello world"), Heading(level=1, text="One")],
    }
    base.update(kw)
    return Document(**base)


def test_content_id_is_stable_and_content_sensitive():
    a, b = _doc(), _doc()
    assert a.content_id == b.content_id
    c = _doc(blocks=[Paragraph(text="hello world!")])
    assert c.content_id != a.content_id


def test_canonical_json_does_not_escape_non_ascii():
    """Failure C5: escaping × broke a correction pipeline silently."""
    d = _doc(blocks=[Paragraph(text="7× wider")])
    assert "×" in d.canonical_json()
    assert "\\u00d7" not in d.canonical_json()


def test_table_must_be_rectangular():
    with pytest.raises(ValueError):
        Table(header=["a", "b"], rows=[["1"]])


# ------------------------------------------------------------------ A1–A4
def test_rolling_hash_detects_single_character_corruption():
    """Failure A1: one byte flipped, nothing reported it."""
    good = "x" * 5000 + "h" + "y" * 600
    bad = "x" * 5000 + "g" + "y" * 600
    assert rolling_hash(good) != rolling_hash(bad)


def test_payload_roundtrip_is_single_gzip_member():
    """Failure A4: two concatenated members break DecompressionStream."""
    text = "hello ×÷— " * 5000
    enc = encode_payload(text)
    assert decode_payload(enc) == text
    import base64

    assert base64.b64decode(enc).count(b"\x1f\x8b\x08") == 1


class _FlakyChannel:
    """Corrupts exactly one character of chunk 1, once."""

    def __init__(self, corrupt: bool = True):
        self.store: dict[str, str] = {}
        self.corrupt = corrupt
        self.calls = 0

    async def push_chunk(self, key, index, data):
        self.calls += 1
        if self.corrupt and index == 1 and self.calls < 4:
            data = data[:-1] + ("Z" if data[-1] != "Z" else "Y")
        self.store[f"{key}:{index}"] = data

    async def remote_hash(self, key):
        v = self.store.get(key)
        return None if v is None else rolling_hash(v)

    async def remote_length(self, key):
        return len(self.store.get(key, ""))

    async def assemble(self, key, count):
        self.store[key] = "".join(self.store.get(f"{key}:{i}", "") for i in range(count))


async def test_transport_retries_corrupted_chunk_then_succeeds():
    ch = _FlakyChannel()
    t = ChunkedTransport(ch, chunk_size=64)
    payload = "abcdefgh" * 40
    await t.send("k", payload, compress=False)
    assert ch.store["k"] == payload


async def test_transport_raises_when_channel_is_persistently_lossy():
    class AlwaysBad(_FlakyChannel):
        async def push_chunk(self, key, index, data):
            self.store[f"{key}:{index}"] = data[:-1] + "Z"

    t = ChunkedTransport(AlwaysBad(), chunk_size=64)
    with pytest.raises(TransportCorruption):
        await t.send("k", "abcdefgh" * 40, compress=False)


async def test_transport_skips_when_remote_already_matches():
    ch = _FlakyChannel(corrupt=False)
    t = ChunkedTransport(ch, chunk_size=64)
    await t.send("k", "payload", compress=False)
    before = ch.calls
    await t.send("k", "payload", compress=False)
    assert ch.calls == before  # idempotent re-push


async def test_chunk_ceiling_is_probed_not_assumed():
    """Failure A2: the real limit was ~6.5KB and undocumented."""
    t = ChunkedTransport(_FlakyChannel(corrupt=False), chunk_size=45_000)

    async def probe(n: int) -> bool:
        return n <= 6_500

    size = await t.probe_ceiling(probe)
    assert size <= 6_500
    assert t.probed


# -------------------------------------------------------------------- B8
def test_anchor_survives_editor_typography():
    """Failure B8: hair spaces and em dashes broke exact matching."""
    rendered = "Where 80 GB goes. The cores never run out — the memory does."
    source = "Where 80 GB goes. The cores never run out - the memory does."
    assert normalise(rendered) == normalise(source)
    assert find_anchor([rendered], source) == 0


def test_anchor_does_not_reuse_an_index():
    cands = ["same caption", "same caption"]
    first = find_anchor(cands, "same caption")
    second = find_anchor(cands, "same caption", used={first})
    assert first != second


def test_anchor_returns_minus_one_when_nothing_is_close():
    assert find_anchor(["totally different"], "Where 80 GB goes") == -1


# -------------------------------------------------------------------- B9
def test_select_tags_drops_over_long_tags():
    """Failure B9: one 40-char invalid tag instead of five good ones."""
    spec = TagSpec(max_count=3, max_len=10)
    assert select_tags(["ok", "alsofine", "waytoolongtagname", "third", "fourth"], spec) == [
        "ok",
        "alsofine",
        "third",
    ]
    assert select_tags(["a"], None) == []


# ----------------------------------------------------------- capabilities
def test_medium_like_platform_degrades_tables_to_images():
    doc = _doc(
        blocks=[Table(header=["a"], rows=[["1"]]), Table(header=["b"], rows=[["2"]])],
    )
    caps = Capabilities(tables=False, image_upload="browser_paste")
    p = plan(doc, "medium", caps)
    kinds = {d.kind for d in p.degradations}
    assert DegradationKind.TABLE_TO_IMAGE in kinds
    assert len(p.generated_assets) == 2
    assert p.ok


def test_platform_without_images_blocks_a_document_with_figures():
    doc = _doc(
        blocks=[Figure(asset_id="f", caption="c")],
        assets={"f": Asset(id="f", path=Path("x.png"))},
    )
    p = plan(doc, "text-only", Capabilities(image_upload="none"))
    assert not p.ok
    assert "figure" in p.blocking[0]


def test_long_body_splits_into_a_thread_when_supported():
    doc = _doc(blocks=[Paragraph(text="word " * 4000)])
    p = plan(doc, "x", Capabilities(max_body_chars=280, threads=True, image_upload="api"))
    assert DegradationKind.SPLIT_INTO_THREAD in {d.kind for d in p.degradations}
    assert p.ok


# ---------------------------------------------------------------- checks
def test_placeholder_check_blocks_unresolved_cross_links():
    """Failure C3: URL-PART-2 nearly reached a live post."""
    doc = _doc(blocks=[Paragraph(text="See [Part 2](URL-PART-2) for more.")])
    res = run_checks(doc)
    assert any(f.check == "placeholders" and f.severity is Severity.ERROR for f in res.findings)


def test_numeric_check_catches_a_transposed_ratio():
    """Failure C1: 18× and 7× swapped places during an edit."""
    doc = _doc(
        blocks=[
            Paragraph(text="<!-- pubkit:define nvlink = 900 --><!-- pubkit:define pcie5 = 128 -->"),
            Paragraph(text="NVLink is 18× wider. <!-- pubkit:assert 18x = nvlink/pcie5 ±10% -->"),
        ]
    )
    res = run_checks(doc)
    errs = [f for f in res.findings if f.check == "numeric"]
    assert errs and "7.03" in errs[0].message


def test_numeric_check_passes_a_correct_ratio():
    doc = _doc(
        blocks=[
            Paragraph(text="<!-- pubkit:define nvlink = 900 --><!-- pubkit:define pcie5 = 128 -->"),
            Paragraph(text="NVLink is 7× wider. <!-- pubkit:assert 7x = nvlink/pcie5 ±10% -->"),
        ]
    )
    assert not [f for f in run_checks(doc).findings if f.check == "numeric"]


def test_xref_check_flags_a_part_that_does_not_exist():
    """Failure C2: references broke when one article became three."""
    doc = _doc(series=SeriesRef(id="s", index=1, of=3), blocks=[Paragraph(text="As shown in Part 7.")])
    res = run_checks(doc)
    assert any(f.check == "xref" and f.severity is Severity.ERROR for f in res.findings)


def test_budget_check_warns_on_overshoot():
    """Failure C4: 16,388 words against a 12,000 target."""
    doc = _doc(blocks=[Paragraph(text="word " * 3000)], budget=Budget(words=1000))
    assert any(f.check == "budget" for f in run_checks(doc).findings)


def test_missing_asset_is_an_error():
    doc = _doc(blocks=[Figure(asset_id="nope", caption="c")])
    res = run_checks(doc)
    assert any(f.check == "assets" and f.severity is Severity.ERROR for f in res.findings)


# ------------------------------------------------------------ idempotency
def test_state_store_prevents_duplicate_drafts(tmp_path):
    """Failure D2: a naive re-run creates a second draft."""
    s = StateStore(tmp_path / "s.sqlite")
    assert s.needs_update("d1", "medium", "abc")
    s.remember_remote("d1", "medium", "abc", "ref-1", "https://x/1")
    assert not s.needs_update("d1", "medium", "abc")
    assert s.needs_update("d1", "medium", "def")  # content changed


def test_resume_point_is_the_first_incomplete_step(tmp_path):
    """Failure D1: the bridge dropped eight times."""
    s = StateStore(tmp_path / "s.sqlite")
    s.record_step("r", "d1", "medium", Step.AUTH, Status.DONE)
    s.record_step("r", "d1", "medium", Step.DRAFT, Status.DONE)
    s.record_step("r", "d1", "medium", Step.CONTENT, Status.FAILED)
    assert s.resume_point("r", "d1", "medium") is Step.CONTENT


# -------------------------------------------------------------- rendering
def test_editor_html_never_emits_an_img_tag():
    """Failure B5: pasted images are stripped, so we never paste them."""
    doc = _doc(
        blocks=[Figure(asset_id="f", caption="A caption")],
        assets={"f": Asset(id="f", path=Path("a.png"), alt="alt")},
    )
    html = EditorHtmlRenderer().render(doc)
    assert "<img" not in html and "data:" not in html
    assert "A caption" in html


def test_caption_anchors_are_in_document_order():
    doc = _doc(
        blocks=[
            Figure(asset_id="a", caption="first"),
            Paragraph(text="between"),
            Figure(asset_id="b", caption="second"),
        ],
        assets={"a": Asset(id="a", path=Path("a.png")), "b": Asset(id="b", path=Path("b.png"))},
    )
    assert EditorHtmlRenderer().caption_anchors(doc) == [("a", "first"), ("b", "second")]


def test_markdown_renderer_keeps_tables_native():
    doc = _doc(blocks=[Table(header=["h1", "h2"], rows=[["a", "b"]], align=["l", "r"])])
    md = MarkdownRenderer().render(doc)
    assert "| h1 | h2 |" in md and "---:" in md


def test_thread_renderer_respects_the_character_limit():
    doc = _doc(blocks=[Paragraph(text="Sentence number one is here. " * 60)])
    tweets = ThreadRenderer(limit=280).render(doc)
    assert tweets and all(len(t) <= 280 for t in tweets)
    assert tweets[-1].endswith(f"({len(tweets)}/{len(tweets)})")


# ----------------------------------------------------------------- loader
MD = textwrap.dedent(
    """\
    ---
    id: part-1
    title: The Hardware
    subtitle: why it is a memory problem
    tags: [AI Infrastructure, GPU]
    series: {id: inside-ai, index: 1, of: 3}
    budget: {words: 3500}
    ---

    # What is behind the box

    A paragraph with **bold** and `code`.

    ## A subsection

    | Model | Size |
    | :--- | ---: |
    | 7B | 14 GB |
    *Model files, in the sizes you actually meet*

    ![a gpu](img/gpu.gif)
    *A processor and its memory sit apart*

    ```python
    print("hi")
    ```

    > [!key] Design rule
    > Batching is not an optimisation.
    """
)


def test_loader_produces_the_expected_ir(tmp_path):
    p = tmp_path / "part-1.md"
    p.write_text(MD)
    (tmp_path / "img").mkdir()
    (tmp_path / "img" / "gpu.gif").write_bytes(b"GIF89a")

    doc = load_document(p)
    kinds = [b.type.value for b in doc.blocks]
    assert kinds == ["heading", "paragraph", "heading", "table", "figure", "code", "callout"]
    assert doc.series.index == 1 and doc.series.of == 3
    assert doc.budget.words == 3500
    assert doc.tables[0].caption == "Model files, in the sizes you actually meet"
    assert doc.figures[0].caption == "A processor and its memory sit apart"
    assert doc.assets["fig-01"].animated


def test_section_numbers_are_generated_not_authored():
    """Failure C2: hand-numbering made 'Part 1' ambiguous."""
    doc = _doc(
        blocks=[
            Heading(level=1, text="A"),
            Heading(level=2, text="A1"),
            Heading(level=1, text="B"),
        ]
    )
    number_sections(doc)
    assert [b.number for b in doc.blocks] == ["1", "1.1", "2"]


def test_series_numbering_continues_across_documents(tmp_path):
    for i in (1, 2):
        (tmp_path / f"p{i}.md").write_text(
            f"---\nid: p{i}\ntitle: T{i}\nseries: {{id: s, index: {i}, of: 2}}\n---\n\n# Chapter\n\ntext\n"
        )
    from pubkit.core.loader import load_series

    series = load_series(sorted(tmp_path.glob("*.md")))
    first = [b for b in series.documents[0].blocks if b.type.value == "heading"][0]
    second = [b for b in series.documents[1].blocks if b.type.value == "heading"][0]
    assert first.number == "1" and second.number == "2"


def test_thread_renderer_strips_markdown():
    """Plain-text platforms render **bold** literally, which looks like a bug."""
    from pubkit.render.html import plain

    assert plain("A **formula** plus `weights` and a [link](http://x)") == "A formula plus weights and a link"
    assert plain("text <!-- pubkit:define a = 1 --> more") == "text more"


async def test_missing_credentials_are_not_retried():
    """Backing off four times before saying `run pubkit auth login` is waste."""
    from pubkit.core.adapter import with_retry
    from pubkit.core.auth import CredentialError

    calls = 0

    async def fn():
        nonlocal calls
        calls += 1
        raise CredentialError("no token for devto")

    with pytest.raises(CredentialError):
        await with_retry(fn, attempts=4)
    assert calls == 1


async def test_transient_errors_are_retried():
    from pubkit.core.adapter import AdapterError, with_retry

    calls = 0

    async def fn():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise AdapterError("flaky")
        return "ok"

    assert await with_retry(fn, attempts=4, base=0.001) == "ok"
    assert calls == 3
