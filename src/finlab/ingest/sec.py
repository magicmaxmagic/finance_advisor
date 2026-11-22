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
import json
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Any, Self

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from finlab.config import Settings, get_settings

COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"


class SecEtiquetteError(RuntimeError):
    """Refusing to make a request that would violate SEC access rules."""


class SecPayloadError(ValueError):
    """A successful HTTP response did not contain the expected SEC document."""


@dataclass(frozen=True)
class SecFailure:
    code: str
    retryable: bool
    retry_after: float | None = None


def retry_after(response: httpx.Response) -> float | None:
    header = response.headers.get("Retry-After")
    if not header:
        return None
    if header.isascii() and header.isdigit() and len(header) <= 10:
        return float(header)
    try:
        when = parsedate_to_datetime(header)
        return max(0.0, (when - datetime.now(UTC)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None


def sec_failure(exc: Exception) -> SecFailure:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 429:
            return SecFailure("RATE_LIMITED", True, retry_after(exc.response))
        if status == 408 or 500 <= status < 600:
            return SecFailure(f"HTTP_{status}", True, retry_after(exc.response))
        return SecFailure(f"HTTP_{status}", False)
    if isinstance(exc, httpx.TimeoutException):
        return SecFailure("TIMEOUT", True)
    if isinstance(exc, httpx.TransportError):
        return SecFailure("TRANSPORT_ERROR", True)
    return SecFailure("INVALID_PAYLOAD", False)


def decode_sec_json(body: bytes, cik: int | None = None) -> dict:
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict) or (cik is not None and int(payload["cik"]) != cik):
            raise ValueError
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        raise SecPayloadError("Invalid SEC JSON or issuer identity") from exc
    return payload


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
            while (delay := self._next_start - time.monotonic()) > 0:
                await asyncio.sleep(delay)
            self._next_start = time.monotonic() + self._interval

    def cooldown(self, seconds: float) -> None:
        self._next_start = max(self._next_start, time.monotonic() + seconds)


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
        self._blocked: httpx.HTTPStatusError | None = None

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

        backoff = wait_exponential(multiplier=1, min=1, max=30)

        def should_retry(exc):
            failure = sec_failure(exc)
            return self._blocked is None and failure.retryable and (failure.retry_after or 0) <= 30

        def wait(attempt):
            failure = sec_failure(attempt.outcome.exception())
            return max(backoff(attempt), failure.retry_after or 0)

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=wait,
            retry=retry_if_exception(should_retry),
            reraise=True,
        ):
            with attempt:
                async with self._semaphore:
                    if self._blocked is not None:
                        raise self._blocked
                    await self._limiter.acquire()
                    if self._blocked is not None:
                        raise self._blocked
                    response = await self._client.get(url)
                    if response.status_code == 404:
                        return None
                    try:
                        response.raise_for_status()
                    except httpx.HTTPStatusError as exc:
                        failure = sec_failure(exc)
                        if response.status_code == 429:
                            self._limiter.cooldown(failure.retry_after or 1)
                        if response.status_code in {401, 403} or (failure.retry_after or 0) > 30:
                            self._blocked = exc
                        raise
                return response.content
        return None  # pragma: no cover - reraise=True makes this unreachable

    async def get_many(self, urls: Iterable[str]) -> list[tuple[str, bytes | None]]:
        """Fetch concurrently, bounded by the client's semaphore and rate limit."""

        async def one(url: str) -> tuple[str, bytes | None]:
            return url, await self.get_bytes(url)

        return list(await asyncio.gather(*(one(url) for url in urls)))


def companyfacts_url(cik: int) -> str:
    return COMPANYFACTS_URL.format(cik=cik)


def submissions_url(cik: int) -> str:
    return SUBMISSIONS_URL.format(cik=cik)


def get_once(url: str, settings: Settings | None = None, *, timeout: float = 60.0) -> bytes:
    """One synchronous fetch carrying the configured contact address.

    For the handful of once-a-day files — the symbol directory, the ticker
    mapping — where spinning up the async client and its rate limiter would be
    ceremony around a single request. The User-Agent check still applies,
    because that is the part the SEC actually enforces.
    """
    cfg = settings or get_settings()
    if cfg.sec_user_agent_is_placeholder:
        raise SecEtiquetteError(
            "FINLAB_SEC_USER_AGENT is still the placeholder from .env.example. "
            "Set a real contact address before making requests."
        )
    response = httpx.get(
        url,
        headers={"User-Agent": cfg.sec_user_agent, "Accept-Encoding": "gzip, deflate"},
        timeout=timeout,
        follow_redirects=True,
    )
    response.raise_for_status()
    return response.content


def describe_limits(settings: Settings | None = None) -> dict[str, Any]:
    cfg = settings or get_settings()
    return {
        "user_agent": cfg.sec_user_agent,
        "max_rps": cfg.sec_max_rps,
        "max_concurrency": cfg.sec_max_concurrency,
    }
