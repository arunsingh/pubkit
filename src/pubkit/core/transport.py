# Copyright 2026 The pubkit Authors
# SPDX-License-Identifier: Apache-2.0
"""Verified chunked transport.

Written because a 12,368-character payload once arrived with exactly one byte
mutated (`h` → `g`) and nothing anywhere reported an error. gzip failed three
steps later with `invalid distance too far back`, which is a spectacularly
unhelpful way to learn your channel is lossy.

The rules this module enforces:

  A1  every chunk is hash-verified end to end
  A2  the per-call size ceiling is *probed*, never assumed
  A3  staging survives navigation, and re-pushing is a no-op
  A4  exactly one gzip member per payload
"""
from __future__ import annotations

import base64
import gzip
import io
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

# The rolling hash below is deliberately trivial and identical on both sides:
# a 32-bit FNV-ish accumulation that a page can compute in three lines of JS
# without pulling in a crypto library. It is an integrity check against a lossy
# channel, not a security primitive.
_MASK = 0xFFFFFFFF


def rolling_hash(s: str) -> str:
    h = 0
    for ch in s:
        h = (h * 31 + ord(ch)) & _MASK
    return format(h, "x")


#: The JavaScript twin of :func:`rolling_hash`. Kept next to it on purpose —
#: if one changes and the other does not, verification breaks silently, which
#: is the exact failure this module exists to prevent.
ROLLING_HASH_JS = """
window.__pkHash = function (s) {
  let h = 0;
  for (let i = 0; i < s.length; i++) { h = (h * 31 + s.charCodeAt(i)) >>> 0; }
  return h.toString(16);
};
"""


def encode_payload(text: str) -> str:
    """gzip + base64, guaranteed single-member (failure A4).

    `gzip.compress` emits one member; concatenating two independently
    compressed halves does not, and `DecompressionStream('gzip')` rejects that
    with `Junk found after end of compressed data` while Python's
    `gzip.decompress` happily accepts it. Divergent behaviour between the two
    ends of a pipe is how a bug hides for an hour.
    """
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as fh:
        fh.write(text.encode("utf-8"))
    raw = buf.getvalue()
    assert raw.count(b"\x1f\x8b") >= 1, "not gzip"
    return base64.b64encode(raw).decode("ascii")


def decode_payload(b64: str) -> str:
    return gzip.decompress(base64.b64decode(b64)).decode("utf-8")


class Channel(Protocol):
    """Anything that can move a string to the far side and run code there."""

    async def push_chunk(self, key: str, index: int, data: str) -> None: ...
    async def remote_hash(self, key: str) -> str | None: ...
    async def remote_length(self, key: str) -> int: ...
    async def assemble(self, key: str, count: int) -> None: ...


@dataclass
class ChunkedTransport:
    """Move a large payload across a channel with an unknown, undocumented
    per-call size ceiling, and prove it arrived intact."""

    channel: Channel
    #: Starting guess. The real ceiling is discovered, not trusted.
    chunk_size: int = 5_600
    max_chunk: int = 45_000
    min_chunk: int = 1_024
    probed: bool = False
    _stats: dict = field(default_factory=dict)

    async def probe_ceiling(self, probe: Callable[[int], Awaitable[bool]]) -> int:
        """Binary-search the largest chunk the channel accepts (failure A2).

        Called once per channel; the result belongs in the state store so the
        next run starts from the known-good value.
        """
        lo, hi = self.min_chunk, self.max_chunk
        best = self.min_chunk
        while lo <= hi:
            mid = (lo + hi) // 2
            if await probe(mid):
                best, lo = mid, mid + 1
            else:
                hi = mid - 1
        # Back off 20%: the ceiling is not always stable under load, and a
        # chunk that fails mid-run costs a retry plus a reassembly.
        self.chunk_size = max(self.min_chunk, int(best * 0.8))
        self.probed = True
        return self.chunk_size

    async def send(self, key: str, text: str, *, compress: bool = True) -> str:
        """Push `text` under `key`, verify it, and return the payload hash.

        Idempotent: if the far side already holds a payload with the right hash
        the whole thing is a no-op, which is what makes a dropped bridge cost
        one step rather than one run (failure D1).
        """
        payload = encode_payload(text) if compress else text
        want = rolling_hash(payload)

        existing = await self.channel.remote_hash(key)
        if existing == want:
            self._stats["skipped"] = self._stats.get("skipped", 0) + 1
            return want

        chunks = [payload[i : i + self.chunk_size] for i in range(0, len(payload), self.chunk_size)]
        for i, chunk in enumerate(chunks):
            await self._push_verified(key, i, chunk)

        await self.channel.assemble(key, len(chunks))

        got = await self.channel.remote_hash(key)
        if got != want:
            remote_len = await self.channel.remote_length(key)
            raise TransportCorruption(
                f"payload {key!r} corrupted in transit: "
                f"sent {len(payload)} chars hash={want}, "
                f"remote has {remote_len} chars hash={got}"
            )
        self._stats["sent"] = self._stats.get("sent", 0) + len(chunks)
        return want

    async def _push_verified(self, key: str, index: int, chunk: str, *, attempts: int = 3) -> None:
        want = rolling_hash(chunk)
        last: str | None = None
        for _ in range(attempts):
            await self.channel.push_chunk(key, index, chunk)
            got = await self.channel.remote_hash(f"{key}:{index}")
            if got == want:
                return
            last = got
        raise TransportCorruption(
            f"chunk {index} of {key!r} failed {attempts} verification attempts "
            f"(want {want}, got {last}); channel is lossy at {len(chunk)} chars"
        )


class TransportCorruption(RuntimeError):
    """The channel altered the payload. Never retry blindly past this."""


def crc(text: str) -> int:
    """A second, independent checksum. Used where a payload crosses two hops
    and you want to know *which* hop ate it."""
    return zlib.crc32(text.encode("utf-8")) & _MASK
