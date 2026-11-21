"""SEC cache isolation and PIT rules, using wire-format fixture documents only."""

from __future__ import annotations

import gzip
import json
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from finlab.ingest import companyfacts, live_facts
from finlab.ingest.live_facts import FundamentalsError, load_facts
from finlab.ingest.universe import Company
from finlab.research.live_demo import demo_inputs
from finlab.research.weekly import screen

WEEK = date(2026, 11, 20)
ALPHA = Company(1, "AAA", "Industrials", "Alpha")
BETA = Company(2, "BBB", "Technology", "Beta")


def _observation(year: int, value: float, filed: str | None = None) -> dict:
    return {
        "start": f"{year}-01-01",
        "end": f"{year}-12-31",
        "form": "10-K",
        "filed": filed or f"{year + 1}-02-20",
        "val": value,
        "accn": f"annual-{year}-{filed or 'original'}",
    }


def _cache(settings, company: Company, *, age_hours: int = 0) -> None:
    doc = {
        "cik": company.cik,
        "facts": {
            "us-gaap": {
                "Revenues": {"units": {"USD": [_observation(2024, 100), _observation(2025, 120)]}},
                "EarningsPerShareDiluted": {
                    "units": {
                        "USD/shares": [
                            _observation(2025, 4),
                            _observation(2025, 900, str(WEEK)),
                            _observation(2025, 999, "2026-12-01"),
                        ]
                    }
                },
            }
        },
    }
    target = companyfacts.raw_path(settings, company.cik)
    target.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(target, "wt") as handle:
        json.dump(doc, handle)
    manifest = companyfacts.load_manifest(settings)
    manifest[str(company.cik)] = {
        "sha256": f"fixture-{company.cik}",
        "fetched_at": (datetime.now(UTC) - timedelta(hours=age_hours)).isoformat(),
    }
    companyfacts.save_manifest(settings, manifest)


def test_fresh_cache_never_refetches_and_excludes_same_day(settings, monkeypatch):
    _cache(settings, ALPHA)

    async def forbidden(*args, **kwargs):
        pytest.fail("fresh cache must not make network requests")

    monkeypatch.setattr(live_facts, "fetch_poc_universe", forbidden)
    facts, hashes = load_facts(settings, (ALPHA,), WEEK)
    assert facts[1]["diluted_eps"][0].value == 4
    assert all(f.filed < WEEK for values in facts[1].values() for f in values)
    assert hashes == {"AAA": "fixture-1"}


def test_watchlist_change_only_downloads_new_or_expired_issuers(settings, monkeypatch):
    _cache(settings, ALPHA)
    seen = []

    async def fetch(companies, cfg, *, force):
        assert force and cfg is settings
        seen.extend(companies)
        for company in companies:
            _cache(settings, company)
        return companyfacts.FetchReport(downloaded=[c.cik for c in companies])

    monkeypatch.setattr(live_facts, "fetch_poc_universe", fetch)
    facts, _ = load_facts(settings, (ALPHA, BETA), WEEK)
    assert seen == [BETA]
    assert set(facts) == {1, 2}
    _cache(settings, ALPHA, age_hours=2)
    load_facts(settings, (ALPHA,), WEEK)
    assert seen == [BETA, ALPHA]


def test_404_does_not_reuse_the_previous_raw_document(settings, monkeypatch):
    _cache(settings, ALPHA, age_hours=2)

    async def missing(*args, **kwargs):
        return companyfacts.FetchReport(missing=[1])

    monkeypatch.setattr(live_facts, "fetch_poc_universe", missing)
    assert load_facts(settings, (ALPHA,), WEEK) == ({}, {})


def test_rate_limit_fails_explicitly_without_expired_fallback(settings, monkeypatch):
    _cache(settings, ALPHA, age_hours=2)

    async def limited(*args, **kwargs):
        request = httpx.Request("GET", "https://data.sec.gov/fixture")
        response = httpx.Response(429, request=request)
        raise httpx.HTTPStatusError(
            "sensitive upstream message", request=request, response=response
        )

    monkeypatch.setattr(live_facts, "fetch_poc_universe", limited)
    with pytest.raises(FundamentalsError, match="429") as error:
        load_facts(settings, (ALPHA,), WEEK)
    assert "sensitive" not in str(error.value)


def test_missing_sec_identity_is_explicit(settings):
    with pytest.raises(FundamentalsError, match="FINLAB_SEC_USER_AGENT"):
        load_facts(settings, (ALPHA,), WEEK)


def test_demo_is_deterministic_with_all_four_criteria_passing():
    first = demo_inputs((ALPHA, BETA), WEEK)
    assert first == demo_inputs((ALPHA, BETA), WEEK)
    bars, facts = first
    rows = screen([ALPHA, BETA], bars, facts, WEEK)
    assert all(r["status"] == "Buy candidate" for r in rows)
    assert all(r["price"] == 99 and r["yield_price_ceiling"] == 100 for r in rows)
    assert all(r["history"]["windows"] == 20 for r in rows)
