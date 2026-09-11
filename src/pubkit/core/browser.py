# Copyright 2026 The pubkit Authors
# SPDX-License-Identifier: Apache-2.0
"""BrowserAdapter — driving a rich-text editor that was never meant to be driven.

Everything in this module was learned the hard way against Medium's editor.
Five things matter, and they generalise to Substack, Ghost's editor, LinkedIn
articles and anything else built on contenteditable:

1.  **Replace by pasting over a selection, never by deleting everything.**
    `execCommand('delete')` across the whole document destroys the editor's
    internal scaffolding; afterwards pastes land nowhere and the only recovery
    is a reload.

2.  **Trust nothing until it survives a reload.** A delete that took 355
    paragraphs to 189 came back as 339. A link href set directly in the DOM
    reverted completely. The editor keeps its own model and syncs deltas.

3.  **Images cannot travel inside pasted HTML.** `data:` URIs are stripped and
    so are ordinary `https://` URLs. The only path that works is a synthetic
    `ClipboardEvent` carrying a real `File` in its `DataTransfer`, which makes
    the editor run its own upload.

4.  **Getting bytes into the page is the actual hard problem.** An injected
    `<input type=file>` plus the automation layer's file-upload primitive
    solves it without ever opening a native picker.

5.  **Anchors shift.** Every insert renumbers the document. Re-resolve by text
    after every mutation, never cache an index.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .adapter import BaseAdapter, Fingerprint, VerificationFailed
from .anchors import AnchorNotFound, find_anchor, similarity
from .transport import ROLLING_HASH_JS

log = logging.getLogger(__name__)


class DocumentReplaceMismatch(RuntimeError):
    """Paste-replace produced the wrong paragraph count — almost always means
    the selection covered part of the document (failure B1)."""


@dataclass
class EditorSelectors:
    """Everything platform-specific about an editor, in one place.

    A new browser platform is mostly this dataclass plus a publish click.
    """

    editable: str
    #: All content roots. Getting this wrong — grabbing only the first — is what
    #: made a paste insert instead of replace (failure B1).
    content_roots: str
    block: str
    figure: str
    figure_img: str
    link: str
    publish_button: str
    tag_input: str | None = None
    tag_chip: str | None = None
    confirm_publish: str | None = None


UPLOAD_INPUT_ID = "__pubkit_upload"

_JS_HELPERS = (
    ROLLING_HASH_JS
    + """
window.__pk = window.__pk || {};

window.__pk.roots = function (sel) {
  return Array.from(document.querySelectorAll(sel));
};

window.__pk.blocks = function (rootSel, blockSel) {
  // Across ALL roots, not just the first. This single detail is the
  // difference between replacing a document and duplicating it.
  const out = [];
  for (const root of window.__pk.roots(rootSel)) {
    out.push(...Array.from(root.querySelectorAll(blockSel)));
  }
  return out;
};

window.__pk.texts = function (rootSel, blockSel) {
  return window.__pk.blocks(rootSel, blockSel).map(e => (e.innerText || '').trim());
};

window.__pk.selectAll = function (rootSel, blockSel) {
  const g = window.__pk.blocks(rootSel, blockSel);
  if (!g.length) return 0;
  const r = document.createRange();
  r.setStartBefore(g[0]);
  r.setEndAfter(g[g.length - 1]);
  const s = getSelection();
  s.removeAllRanges();
  s.addRange(r);
  return g.length;
};

window.__pk.caretAtEndOf = function (rootSel, blockSel, index) {
  const g = window.__pk.blocks(rootSel, blockSel);
  if (index < 0 || index >= g.length) return false;
  const r = document.createRange();
  r.selectNodeContents(g[index]);
  r.collapse(false);
  const s = getSelection();
  s.removeAllRanges();
  s.addRange(r);
  return true;
};

window.__pk.pasteHtml = function (editableSel, html) {
  const ed = document.querySelector(editableSel);
  ed.focus();
  const dt = new DataTransfer();
  dt.setData('text/html', html);
  ed.dispatchEvent(new ClipboardEvent('paste', {
    clipboardData: dt, bubbles: true, cancelable: true,
  }));
  return true;
};

window.__pk.pasteFile = function (editableSel, fileIndex) {
  // The ONLY image path that survives. Remote URLs and data: URIs are
  // stripped from pasted HTML; a real File in DataTransfer.items makes the
  // editor run its own uploader and put the result on its CDN.
  const input = document.getElementById('%UPLOAD_ID%');
  if (!input || !input.files || !input.files[fileIndex]) return 'nofile';
  const ed = document.querySelector(editableSel);
  ed.focus();
  const dt = new DataTransfer();
  dt.items.add(input.files[fileIndex]);
  ed.dispatchEvent(new ClipboardEvent('paste', {
    clipboardData: dt, bubbles: true, cancelable: true,
  }));
  return 'ok';
};

window.__pk.ensureUploadInput = function () {
  let i = document.getElementById('%UPLOAD_ID%');
  if (!i) {
    i = document.createElement('input');
    i.type = 'file';
    i.id = '%UPLOAD_ID%';
    i.multiple = true;
    // Visible enough to be a real element, invisible enough not to matter.
    i.style.cssText =
      'position:fixed;top:0;left:0;z-index:2147483647;opacity:0.01;width:80px;height:24px';
    document.body.appendChild(i);
  }
  return true;
};

window.__pk.stage = function (key, index, data) {
  sessionStorage.setItem(key + ':' + index, data);
  return window.__pkHash(data);
};

window.__pk.stagedHash = function (key) {
  const v = sessionStorage.getItem(key);
  return v === null ? null : window.__pkHash(v);
};

window.__pk.assemble = function (key, count) {
  let out = '';
  for (let i = 0; i < count; i++) out += sessionStorage.getItem(key + ':' + i) || '';
  sessionStorage.setItem(key, out);
  for (let i = 0; i < count; i++) sessionStorage.removeItem(key + ':' + i);
  return window.__pkHash(out);
};

window.__pk.inflate = async function (key) {
  // Single gzip member only; concatenated members throw here even though
  // Python's gzip.decompress accepts them.
  const b64 = sessionStorage.getItem(key);
  const bin = atob(b64);
  const a = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) a[i] = bin.charCodeAt(i);
  const ds = new DecompressionStream('gzip');
  const w = ds.writable.getWriter();
  w.write(a); w.close();
  const rd = ds.readable.getReader();
  const parts = []; let n = 0;
  for (;;) { const { done, value } = await rd.read(); if (done) break; parts.push(value); n += value.length; }
  const o = new Uint8Array(n); let k = 0;
  for (const p of parts) { o.set(p, k); k += p.length; }
  return new TextDecoder().decode(o);
};

window.__pk.fingerprint = function (rootSel, blockSel, figSel, linkSel, markerRe) {
  const roots = window.__pk.roots(rootSel);
  const text = roots.map(r => r.innerText).join('\\n');
  const blocks = window.__pk.blocks(rootSel, blockSel);
  return {
    words: text.split(/\\s+/).filter(Boolean).length,
    headings: blocks.filter(e => /^H[1-6]$/.test(e.tagName)).map(e => e.innerText.trim().slice(0, 48)),
    images: roots.reduce((n, r) => n + r.querySelectorAll(figSel).length, 0),
    links: roots.reduce((n, r) => n + r.querySelectorAll(linkSel).length, 0),
    markers: (text.match(new RegExp(markerRe, 'g')) || []).length,
  };
};
""".replace("%UPLOAD_ID%", UPLOAD_INPUT_ID)
)


class BrowserAdapter(BaseAdapter):
    """Base class for editor-driven platforms.

    Subclasses supply `selectors`, a login URL, a draft URL builder and a
    publish routine. Everything below is platform-agnostic.
    """

    selectors: EditorSelectors
    login_url: str = ""
    marker_regex: str = r"\[\[\s*IMAGE"   # a JS RegExp source string, not a Python pattern

    def __init__(self) -> None:
        super().__init__()
        self._page: Any = None

    # ------------------------------------------------------------------ page
    async def _eval(self, script: str, *args) -> Any:
        return await self._page.evaluate(script, *args)

    async def install_helpers(self) -> None:
        await self._eval(_JS_HELPERS)
        await self._eval("window.__pk.ensureUploadInput()")

    # ------------------------------------------------------- document replace
    async def replace_document(self, html: str, *, expect_blocks: int | None = None) -> int:
        """Replace the entire document body by pasting over a full selection.

        Deliberately *not* implemented as delete-then-paste. Deleting the whole
        block range breaks the editor badly enough that a reload is the only
        way back (failure B2).
        """
        s = self.selectors
        before = await self._eval(
            "([r,b]) => window.__pk.selectAll(r,b)", [s.content_roots, s.block]
        )
        if not before:
            raise DocumentReplaceMismatch("no blocks found — selectors are wrong or page not ready")

        await self._eval("([e,h]) => window.__pk.pasteHtml(e,h)", [s.editable, html])
        await asyncio.sleep(2.5)

        after = await self._eval(
            "([r,b]) => window.__pk.blocks(r,b).length", [s.content_roots, s.block]
        )
        if expect_blocks is not None and after != expect_blocks:
            raise DocumentReplaceMismatch(
                f"expected {expect_blocks} blocks after replace, got {after} "
                f"(was {before}). If after ≈ before + expected, the selection "
                f"covered only one content root — check `content_roots`."
            )
        log.info("replaced document: %d blocks → %d blocks", before, after)
        return after

    # ----------------------------------------------------------- anchor lookup
    async def block_texts(self) -> list[str]:
        s = self.selectors
        return await self._eval("([r,b]) => window.__pk.texts(r,b)", [s.content_roots, s.block])

    async def resolve_anchor(self, target: str, *, used: set[int] | None = None) -> int:
        texts = await self.block_texts()
        idx = find_anchor(texts, target, used=used)
        if idx < 0:
            scored = sorted(((similarity(t, target), t) for t in texts), reverse=True)
            best = scored[0] if scored else (0.0, None)
            raise AnchorNotFound(target, best[1], best[0])
        return idx

    # ------------------------------------------------------------------ media
    async def insert_image_at(self, caption: str, file_index: int, *, used: set[int] | None = None) -> None:
        """Place an image directly above the paragraph that carries `caption`.

        The figure lands *before* the caret's paragraph, so anchoring on the
        caption gives the right visual result for free (failure B7). Anchors are
        re-resolved every time because every insert shifts the indices.
        """
        s = self.selectors
        idx = await self.resolve_anchor(caption, used=used)
        ok = await self._eval(
            "([r,b,i]) => window.__pk.caretAtEndOf(r,b,i)", [s.content_roots, s.block, idx]
        )
        if not ok:
            raise AnchorNotFound(caption, None, 0.0)

        before = await self._count_figures()
        res = await self._eval("([e,i]) => window.__pk.pasteFile(e,i)", [s.editable, file_index])
        if res == "nofile":
            raise RuntimeError(
                f"no staged file at index {file_index}; call stage_files() first"
            )
        await self._wait_for_figure(before + 1)

    async def _count_figures(self) -> int:
        s = self.selectors
        return await self._eval(
            "([r,f]) => window.__pk.roots(r).reduce((n,x)=>n+x.querySelectorAll(f).length,0)",
            [s.content_roots, s.figure],
        )

    async def _wait_for_figure(self, want: int, timeout: float = 45.0) -> None:
        """Wait for the editor's own uploader to finish.

        Polls for the figure to exist *and* for its src to point at the
        platform's CDN rather than a blob: URL — a blob means the upload has
        not completed and publishing now would produce a broken image.
        """
        s = self.selectors
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            state = await self._eval(
                "([r,f,i]) => { const roots = window.__pk.roots(r);"
                " const figs = roots.flatMap(x => Array.from(x.querySelectorAll(f)));"
                " return { n: figs.length, blobs: figs.filter(g => { const im = g.querySelector(i);"
                " return !im || !im.src || im.src.startsWith('blob:') || im.src.startsWith('data:'); }).length }; }",
                [s.content_roots, s.figure, s.figure_img],
            )
            if state["n"] >= want and state["blobs"] == 0:
                return
            await asyncio.sleep(1.0)
        raise VerificationFailed(
            f"image upload did not settle within {timeout:.0f}s "
            "(figure missing, or src still a blob: URL — the platform has not stored it)"
        )

    async def stage_files(self, paths: Sequence[Path], upload) -> None:
        """Hand real bytes to the page.

        `upload` is the automation layer's file-upload primitive, which sets
        `input.files` on an element. We inject the input ourselves so no native
        file picker is ever opened — a picker is undriveable and blocks the
        whole session.
        """
        await self.install_helpers()
        await upload(f"#{UPLOAD_INPUT_ID}", [str(p) for p in paths])
        n = await self._eval(
            f"() => {{ const i = document.getElementById('{UPLOAD_INPUT_ID}');"
            " return i && i.files ? i.files.length : 0; }"
        )
        if n != len(paths):
            raise RuntimeError(f"staged {n} files but expected {len(paths)}")

    # ------------------------------------------------------------ verification
    async def fingerprint(self) -> Fingerprint:
        s = self.selectors
        raw = await self._eval(
            "([r,b,f,l,m]) => window.__pk.fingerprint(r,b,f,l,m)",
            [s.content_roots, s.block, s.figure, s.link, self.marker_regex],
        )
        return Fingerprint(**raw)

    async def verify_after_reload(self, expected: Fingerprint, reload_fn) -> Fingerprint:
        """Reload, then compare. The only honest verification (failure B3).

        An editor will happily show you a change it has not persisted. Asking
        the server what it actually has is the difference between "published
        correctly" and "published, and broken for every reader".
        """
        await reload_fn()
        await asyncio.sleep(3.0)
        await self.install_helpers()
        actual = await self.fingerprint()
        actual.assert_matches(expected)
        return actual

    # -------------------------------------------------------------------- tags
    async def apply_tags(self, tags: list[str], strategy: str) -> int:
        """Commit tags, then *check* that chips actually appeared.

        Typing `a,b,c` into a framework-controlled tag field once produced a
        single 40-character invalid tag. Publishing without tags is a small
        loss; publishing with one garbage tag is worse (failure B9).
        """
        s = self.selectors
        if not s.tag_input or not tags:
            return 0
        await self._page.click(s.tag_input)
        if strategy == "comma":
            for t in tags:
                await self._page.type(s.tag_input, t + ",", delay=40)
                await asyncio.sleep(0.4)
        elif strategy == "enter":
            for t in tags:
                await self._page.type(s.tag_input, t, delay=40)
                await self._page.keyboard.press("Enter")
                await asyncio.sleep(0.4)
        else:  # native_setter — drive the framework's own value setter
            for t in tags:
                await self._eval(
                    "([sel,val]) => { const el = document.querySelector(sel);"
                    " const proto = Object.getPrototypeOf(el);"
                    " const d = Object.getOwnPropertyDescriptor(proto, 'value');"
                    " d.set.call(el, val);"
                    " el.dispatchEvent(new Event('input', { bubbles: true }));"
                    " el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })); }",
                    [s.tag_input, t],
                )
                await asyncio.sleep(0.4)

        if not s.tag_chip:
            return len(tags)
        chips = await self._eval(f"() => document.querySelectorAll({s.tag_chip!r}).length")
        if chips == 0:
            log.warning(
                "tag strategy %r committed no chips; publishing without tags "
                "rather than risking one malformed tag",
                strategy,
            )
            await self._eval(
                "(sel) => { const el = document.querySelector(sel); if (el) { el.value=''; "
                "el.dispatchEvent(new Event('input',{bubbles:true})); } }",
                s.tag_input,
            )
        return chips
