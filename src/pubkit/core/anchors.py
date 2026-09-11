# Copyright 2026 The pubkit Authors
# SPDX-License-Identifier: Apache-2.0
"""Finding a place in a document you do not control.

Rich-text editors normalise what you give them. A caption written as

    Where 80 GB goes. The cores never run out - the memory does.

came back from the editor as

    Where 80 GB goes. The cores never run out — the memory does.

—hair spaces around an em dash. Exact string equality failed, the anchor was
not found, and one image out of fifteen silently did not get placed. Matching
text against a live editor needs to be forgiving in exactly the ways editors
are aggressive.
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
_QUOTES = {
    ord("‘"): "'", ord("’"): "'", ord("‚"): "'", ord("‛"): "'",
    ord("“"): '"', ord("”"): '"', ord("„"): '"', ord("″"): '"',
}
_SPACES = dict.fromkeys(
    map(ord, "            　"),
    " ",
)


def normalise(s: str) -> str:
    """Fold everything an editor is likely to have changed."""
    s = unicodedata.normalize("NFKC", s)
    s = s.translate(_SPACES).translate(_DASHES).translate(_QUOTES)
    s = s.replace("​", "").replace("﻿", "")
    s = re.sub(r"\s*-\s*", " - ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip().casefold()


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalise(a), normalise(b)).ratio()


def find_anchor(
    candidates: list[str],
    target: str,
    *,
    floor: float = 0.82,
    used: set[int] | None = None,
) -> int:
    """Locate `target` among `candidates`, returning an index or -1.

    Three passes, cheapest first: exact-after-normalisation, prefix, then
    fuzzy above `floor`. `used` lets a caller place several identical captions
    without every one resolving to the first match.
    """
    used = used or set()
    norm = [normalise(c) for c in candidates]
    t = normalise(target)

    for i, c in enumerate(norm):
        if i not in used and c == t:
            return i

    if len(t) >= 16:
        for i, c in enumerate(norm):
            if i not in used and (c.startswith(t[:40]) or t.startswith(c[:40])):
                return i

    best, best_score = -1, floor
    for i, c in enumerate(norm):
        if i in used:
            continue
        score = SequenceMatcher(None, c, t).ratio()
        if score > best_score:
            best, best_score = i, score
    return best


class AnchorNotFound(LookupError):
    def __init__(self, target: str, closest: str | None, score: float) -> None:
        hint = f" closest was {closest!r} at {score:.2f}" if closest else ""
        super().__init__(f"no anchor for {target[:60]!r}.{hint}")
        self.target, self.closest, self.score = target, closest, score
