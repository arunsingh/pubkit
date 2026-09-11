# Copyright 2026 The pubkit Authors
# SPDX-License-Identifier: Apache-2.0
"""ApiAdapter — the easy half.

API platforms are pleasant by comparison: no DOM, no editor model, no stripped
images. What they do have is rate limits, partial failure and the same
idempotency problem, so the base class handles those and nothing else.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from ..core.adapter import AdapterError, BaseAdapter, Context, RateLimited

log = logging.getLogger(__name__)


class ApiAdapter(BaseAdapter):
    base_url: str = ""
    timeout: float = 30.0

    def __init__(self) -> None:
        super().__init__()
        self._client: httpx.AsyncClient | None = None

    def headers(self, ctx: Context) -> dict[str, str]:  # pragma: no cover - overridden
        return {}

    async def client(self, ctx: Context) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                headers={"user-agent": "pubkit/0.1 (+https://github.com/arunsingh/pubkit)", **self.headers(ctx)},
            )
        return self._client

    async def request(self, ctx: Context, method: str, url: str, **kw: Any) -> httpx.Response:
        await self.bucket.acquire()
        client = await self.client(ctx)
        resp = await client.request(method, url, **kw)

        if resp.status_code == 429:
            retry = float(resp.headers.get("retry-after", 30))
            # x-rate-limit-reset is an epoch, not a delta — a detail worth
            # getting right, because treating it as a delta means sleeping
            # until roughly 2056.
            reset = resp.headers.get("x-rate-limit-reset")
            if reset:
                import time

                retry = max(1.0, float(reset) - time.time())
            raise RateLimited(retry)

        if resp.status_code >= 500:
            raise AdapterError(f"{self.name}: {resp.status_code} from {url}")
        if resp.status_code >= 400:
            raise AdapterError(f"{self.name}: {resp.status_code} {resp.text[:300]}")
        return resp

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
