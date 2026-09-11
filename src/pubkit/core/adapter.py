# Copyright 2026 The pubkit Authors
# SPDX-License-Identifier: Apache-2.0
"""The adapter contract, plus the two base classes that do the hard parts.

Six methods is the entire surface a new platform has to implement. Everything
painful — verified transport, anchor resolution, image insertion, fingerprint
verification, rate limiting, retries — lives in the base classes, because those
are exactly the things that went wrong the first time and nobody should have to
rediscover them per platform.
"""
from __future__ import annotations

import asyncio
import hashlib
import random
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .auth import CredentialError, SessionStore, TokenStore
from .capabilities import Capabilities, PublishPlan
from .ir import Document


@dataclass
class RemoteRef:
    """Where a document lives on a platform before publication."""

    id: str
    url: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class PublishedRef:
    id: str
    url: str
    published_at: float = field(default_factory=time.time)


@dataclass
class Fingerprint:
    """A cheap, stable summary of remote state.

    Compared after a reload to prove a mutation actually stuck. Editors lie:
    a delete that visibly worked came back after a refresh, and DOM edits to
    link hrefs never persisted at all (failures B3, B4).
    """

    words: int
    headings: list[str]
    images: int
    links: int
    markers: int = 0

    def digest(self) -> str:
        payload = f"{self.words}|{'|'.join(self.headings)}|{self.images}|{self.links}|{self.markers}"
        return hashlib.blake2b(payload.encode(), digest_size=8).hexdigest()

    def assert_matches(self, expected: Fingerprint, *, tolerance: float = 0.02) -> None:
        problems = []
        if expected.words and abs(self.words - expected.words) / expected.words > tolerance:
            problems.append(f"words {self.words} vs expected {expected.words}")
        if self.images != expected.images:
            problems.append(f"images {self.images} vs expected {expected.images}")
        if self.markers:
            problems.append(f"{self.markers} unresolved marker(s) still present")
        if expected.headings and self.headings[: len(expected.headings)] != expected.headings:
            problems.append("heading list diverges")
        if problems:
            raise VerificationFailed("; ".join(problems))


class VerificationFailed(RuntimeError):
    """Remote state does not match what we pushed. Never publish past this."""


class AdapterError(RuntimeError):
    pass


class NotRetryable(AdapterError):
    """A failure that retrying cannot fix.

    A missing credential is not a transient error. Backing off four times
    before telling the user to run `pubkit auth login` wastes 30 seconds and
    buries the one line that actually helps them.
    """


class RateLimited(AdapterError):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"rate limited, retry after {retry_after:.1f}s")
        self.retry_after = retry_after


@dataclass
class Context:
    """What an adapter is handed. Deliberately narrow.

    An adapter gets credentials for its own platform and nothing else.
    """

    platform: str
    tokens: TokenStore
    sessions: SessionStore
    dry_run: bool = False
    confirm: bool = False
    headless: bool = True
    workdir: str = ".pubkit"
    options: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Adapter(Protocol):
    name: str
    capabilities: Capabilities

    async def authenticate(self, ctx: Context) -> None: ...
    async def ensure_draft(self, doc: Document, ctx: Context) -> RemoteRef: ...
    async def push_content(self, doc: Document, plan: PublishPlan, ref: RemoteRef, ctx: Context) -> None: ...
    async def push_media(self, doc: Document, plan: PublishPlan, ref: RemoteRef, ctx: Context) -> None: ...
    async def verify(self, doc: Document, plan: PublishPlan, ref: RemoteRef, ctx: Context) -> Fingerprint: ...
    async def publish(self, doc: Document, ref: RemoteRef, ctx: Context) -> PublishedRef: ...


# ---------------------------------------------------------------------------
# rate limiting + retry, shared by every adapter
# ---------------------------------------------------------------------------
class TokenBucket:
    def __init__(self, rate: float, burst: int) -> None:
        self.rate, self.burst = rate, burst
        self._tokens = float(burst)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, n: int = 1) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self.burst, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= n:
                    self._tokens -= n
                    return
                await asyncio.sleep((n - self._tokens) / self.rate)


async def with_retry(fn, *, attempts: int = 4, base: float = 1.0, cap: float = 30.0, on_retry=None):
    """Bounded exponential backoff with full jitter.

    Full jitter rather than fixed backoff on purpose: when five platform legs
    fail at once because a shared dependency blipped, synchronised retries make
    it worse.
    """
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return await fn()
        except (NotRetryable, PermissionError, CredentialError):
            raise
        except RateLimited as exc:
            delay = exc.retry_after
        except AdapterError as exc:
            last = exc
            delay = min(cap, base * 2**attempt) * random.random()
        except Exception as exc:  # noqa: BLE001 - adapters raise anything
            last = exc
            delay = min(cap, base * 2**attempt) * random.random()
        else:  # pragma: no cover
            break
        if attempt == attempts - 1:
            break
        if on_retry:
            on_retry(attempt, delay, last)
        await asyncio.sleep(delay)
    raise AdapterError(f"gave up after {attempts} attempts: {last}") from last


class BaseAdapter:
    """Shared plumbing. Adapters subclass either this or a browser/API base."""

    name: str = "base"
    capabilities: Capabilities = Capabilities()
    #: Requests per second and burst, tuned per platform.
    rate: float = 1.0
    burst: int = 3

    def __init__(self) -> None:
        self.bucket = TokenBucket(self.rate, self.burst)

    async def authenticate(self, ctx: Context) -> None:  # pragma: no cover - default
        return None

    async def push_media(self, doc, plan, ref, ctx) -> None:  # pragma: no cover - default
        return None

    def guard_publish(self, ctx: Context) -> None:
        """Publishing is public and hard to undo (failure D3)."""
        if ctx.dry_run:
            raise AdapterError("refusing to publish during a dry run")
        if not ctx.confirm:
            raise AdapterError(
                "publishing requires explicit confirmation: pass --confirm "
                "(or set PUBKIT_CONFIRM=1 in CI)"
            )
