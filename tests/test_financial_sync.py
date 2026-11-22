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


def test_pending_accession_survives_the_rolling_discovery_window(discovery):
    from finlab.engine.status import read_sec_checkpoint

    settings, state, _, body = discovery
    facts = body.pop("facts")
    body["facts"] = {}
    with pytest.raises(financial_sync.SecSyncIncomplete):
        financial_sync.sync_financials(None, settings)
    path = financial_sync.raw_path(settings, 1)
    with gzip.open(path, "wt") as handle:
        json.dump(
            {"filings": {"recent": {"accessionNumber": [], "form": [], "filingDate": []}}}, handle
        )
    with pytest.raises(financial_sync.SecSyncIncomplete):
        financial_sync.sync_financials(None, settings)
    report = read_sec_checkpoint(settings.data_dir / "control")
    assert report["pending"][0]["accession"] == "new"
    assert report["state"] == "degraded"
    body["facts"] = facts
    assert financial_sync.sync_financials(None, settings) == 1
    assert state["1"]["checked_statement_accessions"] == ["new"]
    assert not read_sec_checkpoint(settings.data_dir / "control")["pending"]


@pytest.mark.parametrize(("flag", "requires_facts"), [(0, False), (1, True)])
def test_amendment_is_exempt_only_with_explicit_non_xbrl_evidence(discovery, flag, requires_facts):
    settings, _, calls, body = discovery
    body["facts"] = {}
    path = financial_sync.raw_path(settings, 1)
    with gzip.open(path, "rt") as handle:
        document = json.load(handle)
    document["filings"]["recent"].update(form=["10-Q/A"], isXBRL=[flag])
    with gzip.open(path, "wt") as handle:
        json.dump(document, handle)
    if requires_facts:
        with pytest.raises(financial_sync.SecSyncIncomplete):
            financial_sync.sync_financials(None, settings)
        assert calls
    else:
        assert financial_sync.sync_financials(None, settings) == 0
        assert not calls


def test_interrupted_staging_preserves_accessions_and_blocks_publication(discovery, monkeypatch):
    from datetime import date

    from finlab.engine.prepare import prepare_warehouse
    from finlab.engine.status import read_sec_checkpoint

    settings, manifest, _, _ = discovery
    ingest = financial_sync.ingest_poc

    def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(financial_sync, "ingest_poc", interrupted)
    with pytest.raises(KeyboardInterrupt):
        financial_sync.sync_financials(None, settings)
    assert read_sec_checkpoint(settings.data_dir / "control")["pending"][0]["accession"] == "new"
    assert not manifest["1"]["checked_statement_accessions"]
    with pytest.raises(RuntimeError, match="previous validated warehouse"):
        prepare_warehouse(None, settings, date.today())
    monkeypatch.setattr(financial_sync, "ingest_poc", ingest)
    assert financial_sync.sync_financials(None, settings) == 1
    assert manifest["1"]["checked_statement_accessions"] == ["new"]


def test_invalid_companyfacts_cache_preserves_checkpoint_and_recovers(discovery, monkeypatch):
    from finlab.engine.status import read_sec_checkpoint

    settings, state, calls, body = discovery
    result = {**body, "cik": 2}
    monkeypatch.setattr(financial_sync, "read_raw", lambda *_: result)
    with pytest.raises(financial_sync.SecSyncIncomplete):
        financial_sync.sync_financials(None, settings)
    assert (
        read_sec_checkpoint(settings.data_dir / "control")["pending"][0]["reason"]
        == "COMPANY_FACTS_CACHE_INVALID"
    )
    assert state["1"]["checked_statement_accessions"] == []
    result["cik"] = 1
    assert financial_sync.sync_financials(None, settings) == 1
    assert state["1"]["checked_statement_accessions"] == ["new"]
    assert len(calls) == 2
