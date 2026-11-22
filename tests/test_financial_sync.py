import gzip
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from finlab.engine import financial_sync
from finlab.ingest.universe import Company


@pytest.fixture
def discovery(settings, monkeypatch, tmp_path):
    company = Company(1, "AAA", "Technology", "Issuer")
    filed = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()
    document = {
        "cik": 1,
        "filings": {
            "recent": {"accessionNumber": ["new"], "form": ["10-K"], "filingDate": [filed]}
        },
    }
    path = tmp_path / "submissions.json.gz"
    with gzip.open(path, "wt") as handle:
        json.dump(document, handle)
    state = {"1": {"checked_statement_accessions": []}}
    calls = []
    monkeypatch.setattr(financial_sync, "load_poc_universe", lambda *_: (company,))
    monkeypatch.setattr(financial_sync, "raw_path", lambda *_: path)
    monkeypatch.setattr(financial_sync, "load_manifest", lambda *_: state)
    monkeypatch.setattr(financial_sync, "save_manifest", lambda *_: None)
    monkeypatch.setattr(
        financial_sync, "ingest_submissions", lambda *a, **kw: SimpleNamespace(missing=[])
    )
    monkeypatch.setattr(financial_sync, "load_submissions", lambda *a: None)
    monkeypatch.setattr(
        financial_sync,
        "ingest_poc",
        lambda *a, **kw: SimpleNamespace(fetch=SimpleNamespace(missing=[])),
    )

    async def fetch(companies, cfg, **kwargs):
        calls.append(companies)
        return SimpleNamespace(missing=[])

    monkeypatch.setattr(financial_sync, "fetch_poc_universe", fetch)
    body = {
        "cik": 1,
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "filed": filed,
                                "form": "10-K",
                                "val": 100,
                                "accn": "new",
                            }
                        ]
                    }
                }
            }
        },
    }
    monkeypatch.setattr(financial_sync, "read_raw", lambda *_: body)
    return settings, state, calls, body


def test_unchanged_statement_index_does_not_redownload_all_fundamentals(discovery):
    settings, state, calls, _ = discovery
    assert financial_sync.sync_financials(None, settings) == 1
    assert state["1"]["checked_statement_accessions"] == ["new"]
    assert financial_sync.sync_financials(None, settings) == 0
    assert len(calls) == 1


def test_pending_companyfacts_stays_retryable(discovery):
    settings, state, calls, body = discovery
    body["facts"] = {}
    with pytest.raises(RuntimeError, match="pending"):
        financial_sync.sync_financials(None, settings)
    assert state["1"]["checked_statement_accessions"] == []
    with pytest.raises(RuntimeError, match="pending"):
        financial_sync.sync_financials(None, settings)
    assert len(calls) == 2
