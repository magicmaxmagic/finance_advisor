"""Rate limiters: SEC token bucket, Ollama semaphore of 3.

Two external services with two very different failure modes.

**SEC** publishes a hard ceiling of 10 requests/second and bans IPs that ignore
it. The limit is enforced by spacing request starts, not by a bucket with
capacity, so a burst of queued callers can never briefly exceed it.

**Ollama Cloud Pro** allows exactly 3 concurrent models. Overshooting does not
queue, it rejects — so the semaphore is the design, not an optimisation. A 429
is mapped to a deferral of the job rather than an exception, because quota
exhaustion is a scheduling fact, not an error: it must not consume one of the
job's three attempts and must never kill the worker.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field

import httpx

from finlab.config import Settings, get_settings
from finlab.ingest.sec import RateLimiter


class QuotaExhausted(Exception):
    """The upstream asked us to come back later. Not a failure of the job."""

    def __init__(self, retry_after: float, detail: str = "") -> None:
        super().__init__(detail or f"rate limited; retry in {retry_after:.0f}s")
        self.retry_after = retry_after


def parse_retry_after(response: httpx.Response, *, default: float) -> float:
    """Honour Retry-After when the server sends one, with jitter otherwise.

    Jitter matters: three workers rejected at the same instant would otherwise
    all come back at the same instant.
    """
    header = response.headers.get("Retry-After")
    if header:
        try:
            return max(1.0, float(header))
        except ValueError:
            pass
    return default * random.uniform(0.8, 1.5)


@dataclass
class Pools:
    """The process-wide concurrency limits. One instance per daemon."""

    settings: Settings = field(default_factory=get_settings)
    sec_limiter: RateLimiter = field(init=False)
    sec_slots: asyncio.Semaphore = field(init=False)
    ollama_slots: asyncio.Semaphore = field(init=False)

    def __post_init__(self) -> None:
        self.sec_limiter = RateLimiter(self.settings.sec_max_rps)
        self.sec_slots = asyncio.Semaphore(self.settings.sec_max_concurrency)
        self.ollama_slots = asyncio.Semaphore(self.settings.ollama_max_concurrency)

    async def sec_request(self, client: httpx.AsyncClient, url: str) -> httpx.Response:
        """One SEC request, inside both the concurrency and the rate ceiling."""
        async with self.sec_slots:
            await self.sec_limiter.acquire()
            response = await client.get(url)
        if response.status_code == 429:
            raise QuotaExhausted(parse_retry_after(response, default=60.0), "SEC rate limited")
        return response

    async def ollama_request(
        self, client: httpx.AsyncClient, url: str, payload: dict
    ) -> httpx.Response:
        """One inference call, holding one of the three Pro-tier slots."""
        async with self.ollama_slots:
            response = await client.post(url, json=payload)
        if response.status_code == 429:
            raise QuotaExhausted(
                parse_retry_after(response, default=300.0), "Ollama quota exhausted"
            )
        return response
