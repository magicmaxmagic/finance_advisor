import asyncio

import httpx
import pytest

from finlab.ingest import sec


@pytest.fixture(autouse=True)
def identified_test_client(settings):
    settings.sec_user_agent = "FinLab fixture tests@local.invalid"


def test_recovers_timeout_and_429_then_returns_identified_payload(settings, monkeypatch):
    calls = []
    events = iter(("timeout", 429, 200))

    async def responder(request):
        calls.append(request.headers["User-Agent"])
        event = next(events)
        if event == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        return httpx.Response(event, headers={"Retry-After": "0"}, content=b"ok")

    factory = httpx.AsyncClient
    monkeypatch.setattr(
        sec.httpx,
        "AsyncClient",
        lambda **kw: factory(
            transport=httpx.MockTransport(responder),
            **kw,
        ),
    )
    monkeypatch.setattr(sec, "wait_exponential", lambda **_: lambda _: 0)

    async def run():
        async with sec.SecClient(settings) as client:
            return await client.get_bytes(sec.companyfacts_url(1))

    assert asyncio.run(run()) == b"ok"
    assert calls == [settings.sec_user_agent] * 3


@pytest.mark.parametrize(
    ("status", "header", "attempts", "retryable"),
    [
        (403, {}, 1, False),
        (503, {}, 3, True),
        (429, {"Retry-After": "120"}, 1, True),
    ],
)
def test_permanent_and_persistent_http_failures_are_bounded(
    settings,
    monkeypatch,
    status,
    header,
    attempts,
    retryable,
):
    calls = []
    factory = httpx.AsyncClient

    def responder(request):
        calls.append(request.url)
        return httpx.Response(status, headers=header)

    monkeypatch.setattr(
        sec.httpx,
        "AsyncClient",
        lambda **kw: factory(
            transport=httpx.MockTransport(responder),
            **kw,
        ),
    )
    monkeypatch.setattr(sec, "wait_exponential", lambda **_: lambda _: 0)

    async def run():
        async with sec.SecClient(settings, max_attempts=3) as client:
            with pytest.raises(httpx.HTTPStatusError) as error:
                await client.get_bytes(sec.companyfacts_url(1))
            if status in {403, 429}:
                with pytest.raises(httpx.HTTPStatusError):
                    await client.get_bytes(sec.companyfacts_url(2))
            return sec.sec_failure(error.value)

    failure = asyncio.run(run())
    assert len(calls) == attempts
    assert failure.retryable == retryable
    if status == 429:
        assert failure.retry_after == 120


def test_companyfacts_invalid_issuer_does_not_replace_existing_cache(settings, monkeypatch):
    import gzip
    import json

    from finlab.ingest import companyfacts
    from finlab.ingest.universe import Company

    good = json.dumps(
        {
            "cik": 1,
            "facts": {
                "us-gaap": {
                    "Assets": {
                        "units": {
                            "USD": [
                                {"end": "2025-12-31", "filed": "2026-01-20", "accn": "a", "val": 10}
                            ]
                        }
                    }
                }
            },
        }
    ).encode()
    bodies = {1: good, 2: b'{"cik":999}'}
    factory = httpx.AsyncClient
    monkeypatch.setattr(
        sec.httpx,
        "AsyncClient",
        lambda **kw: factory(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=bodies[int(request.url.path[-15:-5])]),
            ),
            **kw,
        ),
    )
    companies = (Company(1, "ONE", "Tech", "One"), Company(2, "TWO", "Tech", "Two"))
    for _ in range(2):
        report = asyncio.run(companyfacts.fetch_poc_universe(companies, settings, force=True))
        assert report.downloaded == [1]
        assert report.errors[2].code == "INVALID_PAYLOAD"
    saved = companyfacts.load_manifest(settings)
    assert len(saved["1"]["versions"]) == 1
    with gzip.open(companyfacts.raw_path(settings, 1), "rb") as handle:
        assert handle.read() == good
    assert not companyfacts.raw_path(settings, 2).exists()


def test_submissions_resume_repairs_archive_page_without_refetching_index(settings, monkeypatch):
    from datetime import UTC, datetime

    from finlab.ingest.submissions import fetch_submissions, page_path
    from finlab.ingest.universe import Company

    name = "CIK0000000001-submissions-001.json"
    columns = {"accessionNumber": ["verified"], "form": ["10-K"], "filingDate": ["2020-01-01"]}
    index = {"cik": 1, "filings": {"recent": columns, "files": [{"name": name}]}}
    calls = []
    factory = httpx.AsyncClient

    def responder(request):
        calls.append(request.url.path.rsplit("/", 1)[-1])
        if calls[-1] != name:
            return httpx.Response(200, json=index)
        return httpx.Response(404) if calls.count(name) == 1 else httpx.Response(200, json=columns)

    monkeypatch.setattr(
        sec.httpx,
        "AsyncClient",
        lambda **kw: factory(
            transport=httpx.MockTransport(responder),
            **kw,
        ),
    )
    started = datetime.now(UTC)
    companies = (Company(1, "ONE", "Tech", "One"),)
    errors = {}
    assert asyncio.run(fetch_submissions(companies, settings, force=True, errors=errors)) == [1]
    assert errors[1].code == "ARCHIVE_PAGE_MISSING"
    assert (
        asyncio.run(fetch_submissions(companies, settings, force=True, resume_since=started)) == []
    )
    assert calls == ["CIK0000000001.json", name, name]
    assert page_path(settings, name).is_file()
