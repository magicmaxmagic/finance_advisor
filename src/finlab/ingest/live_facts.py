"""Issuer-scoped SEC cache, independent of the 15-second quote refresh.

The SEC Company Facts endpoint has filing dates, not acceptance timestamps.
The live view consequently excludes observations filed on the analysis day.
No failed refresh falls back to expired documents.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta

import httpx

from finlab.config import Settings
from finlab.ingest.companyfacts import fetch_poc_universe, load_manifest, raw_path
from finlab.ingest.sec import SecEtiquetteError
from finlab.ingest.universe import Company
from finlab.research.weekly_report import resolve_annual_facts

CACHE_TTL = timedelta(hours=1)


class FundamentalsError(RuntimeError):
    """Explicit, credential-free error suitable for display in the live UI."""


def _fresh(entry: dict, now: datetime) -> bool:
    try:
        fetched = datetime.fromisoformat(entry["fetched_at"])
        return fetched.tzinfo is not None and timedelta(0) <= now - fetched <= CACHE_TTL
    except (KeyError, TypeError, ValueError):
        return False


def load_facts(
    settings: Settings,
    companies: tuple[Company, ...],
    as_of: date,
    *,
    force: bool = False,
) -> tuple[dict, dict]:
    """Read or refresh only selected issuers; same-day filings are excluded.

    Cache validity refers to when SEC was checked, separately from the fiscal
    period's age. The decision engine still rejects stale annual statements.
    A 404 excludes that issuer even if an old raw document remains on disk.
    """
    if not companies:
        return {}, {}
    now = datetime.now(UTC)
    manifest = load_manifest(settings)
    available = {
        c.cik
        for c in companies
        if not force
        and raw_path(settings, c.cik).is_file()
        and _fresh(manifest.get(str(c.cik), {}), now)
    }
    pending = tuple(c for c in companies if c.cik not in available)
    if pending:
        try:
            fetched = asyncio.run(fetch_poc_universe(pending, settings, force=True))
        except SecEtiquetteError as exc:
            raise FundamentalsError(
                "SEC : configurez FINLAB_SEC_USER_AGENT avec votre adresse de contact."
            ) from exc
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            message = (
                "SEC : limite de débit (429). Réessayez après quelques minutes."
                if code == 429
                else f"SEC : erreur HTTP {code}; aucune donnée périmée utilisée."
            )
            raise FundamentalsError(message) from exc
        except httpx.TransportError as exc:
            raise FundamentalsError(
                "SEC indisponible : erreur réseau; aucune donnée périmée utilisée."
            ) from exc
        available.update(fetched.downloaded)
    if not available:
        return {}, {}
    cutoff = as_of - timedelta(days=1)
    try:
        return resolve_annual_facts(settings, companies, cutoff, available)
    except (ValueError, KeyError, OSError) as exc:
        raise FundamentalsError(
            "SEC : documents annuels absents, incomplets ou invalides pour cette sélection."
        ) from exc
