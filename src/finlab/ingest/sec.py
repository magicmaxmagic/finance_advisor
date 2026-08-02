"""Shared HTTP client for sec.gov.

SEC etiquette is not advisory: requests without a contact address in the
User-Agent are blocked, and sustained traffic above 10 requests/second gets the
source IP throttled. Both limits are enforced here rather than left to each
caller, so there is one place to audit.

M1's bulk loader and M5's filing fetch use this same client; only the URL
changes.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from types import TracebackType
from typing import Any, Self

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from finlab.config import Settings, get_settings

COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"


class SecEtiquetteError(RuntimeError):
    """Refusing to make a request that would violate SEC access rules."""


class RateLimiter:
    """A strict requests-per-second ceiling, shared across concurrent callers.

    Spaces request *starts* by 1/rate rather than refilling a bucket, so a burst
    of queued callers cannot briefly exceed the cap the way a token bucket with
    capacity > 1 would.
    """

    def __init__(self, rate_per_second: float) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        self._interval = 1.0 / rate_per_second
        self._lock = asyncio.Lock()
        self._next_start = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            start_at = max(now, self._next_start)
            self._next_start = start_at + self._interval
            delay = start_at - now
        if delay > 0:
            await asyncio.sleep(delay)


class SecClient:
    """Rate-limited, retrying async client for sec.gov endpoints."""

    def __init__(self, settings: Settings | None = None, *, max_attempts: int = 4) -> None:
        self._settings = settings or get_settings()
        if self._settings.sec_user_agent_is_placeholder:
            raise SecEtiquetteError(
                "FINLAB_SEC_USER_AGENT is still the placeholder from .env.example. "
                "Set a real contact address before making SEC requests — anonymous "
                "traffic is blocked and abusive traffic gets the IP banned."
            )
        self._limiter = RateLimiter(self._settings.sec_max_rps)
        self._semaphore = asyncio.Semaphore(self._settings.sec_max_concurrency)
        self._max_attempts = max_attempts
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> Self:
        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": self._settings.sec_user_agent,
                "Accept-Encoding": "gzip, deflate",
            },
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def get_bytes(self, url: str) -> bytes | None:
        """Fetch a URL. Returns None on 404 — a missing filer is data, not a failure."""
        if self._client is None:
            raise RuntimeError("SecClient must be used as an async context manager")

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=wait_exponential(multiplier=1, min=1, max=30),
            retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
            reraise=True,
        ):
            with attempt:
                async with self._semaphore:
                    await self._limiter.acquire()
                    response = await self._client.get(url)
                if response.status_code == 404:
                    return None
                # 429 and 5xx are retried by raising; 4xx others are terminal.
                if response.status_code == 429 or response.status_code >= 500:
                    response.raise_for_status()
                response.raise_for_status()
                return response.content
        return None  # pragma: no cover - reraise=True makes this unreachable

    async def get_many(self, urls: Iterable[str]) -> list[tuple[str, bytes | None]]:
        """Fetch concurrently, bounded by the client's semaphore and rate limit."""

        async def one(url: str) -> tuple[str, bytes | None]:
            return url, await self.get_bytes(url)

        return list(await asyncio.gather(*(one(url) for url in urls)))


def companyfacts_url(cik: int) -> str:
    return COMPANYFACTS_URL.format(cik=cik)


def describe_limits(settings: Settings | None = None) -> dict[str, Any]:
    cfg = settings or get_settings()
    return {
        "user_agent": cfg.sec_user_agent,
        "max_rps": cfg.sec_max_rps,
        "max_concurrency": cfg.sec_max_concurrency,
    }
