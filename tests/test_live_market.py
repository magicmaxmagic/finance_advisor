"""Alpaca contract tests use synthetic HTTP responses; no credentials or network."""

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

from finlab.config import Settings
from finlab.ingest import live_market as market

CALENDAR = [
    {"date": "2026-11-24", "open": "09:30", "close": "16:00"},
    {"date": "2026-11-25", "open": "09:30", "close": "16:00"},
    # Thanksgiving is deliberately absent; Friday closes early.
    {"date": "2026-11-27", "open": "09:30", "close": "13:00"},
    {"date": "2026-11-30", "open": "09:30", "close": "16:00"},
]
NOW = datetime(2026, 11, 27, 16, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def clear_cooldown():
    market._COOLDOWNS.clear()
    market._CALENDARS.clear()
    yield
    market._COOLDOWNS.clear()
    market._CALENDARS.clear()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        alpaca_key_id=SecretStr("fictional-key"),
        alpaca_secret_key=SecretStr("fictional-secret"),
    )


def test_open_early_close_holiday_weekend_and_preopen():
    session = market.session_from_calendar(CALENDAR, NOW)
    assert session.is_open
    assert session.last_close_date == date(2026, 11, 25)
    assert session.close_at.hour == 13
    assert "écourtée" in session.label
    for now in (
        datetime(2026, 11, 26, 16, tzinfo=UTC),
        datetime(2026, 11, 27, 14, tzinfo=UTC),
    ):
        session = market.session_from_calendar(CALENDAR, now)
        assert not session.is_open
        assert session.trading_date == session.last_close_date == date(2026, 11, 25)
    for now in (
        datetime(2026, 11, 27, 18, tzinfo=UTC),
        datetime(2026, 11, 28, 16, tzinfo=UTC),
    ):
        session = market.session_from_calendar(CALENDAR, now)
        assert not session.is_open
        assert session.last_close_date == date(2026, 11, 27)


def test_missing_keys_never_calls_provider(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path / "data")
    with pytest.raises(market.MarketDataError, match="Clés Alpaca absentes"):
        market.fetch_snapshot(settings, ["AAPL"], NOW)


def test_authentication_failure_is_explicit_and_secret_free(settings):
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(401, text="fictional-secret"))
    )
    with client, pytest.raises(market.MarketDataError, match="authentification") as error:
        market.fetch_snapshot(settings, ["AAA"], NOW, client=client)
    assert "fictional-secret" not in str(error.value)


def test_rate_limit_blocks_subsequent_refreshes(settings):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"}, text="fictional-secret")

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(market.MarketDataError, match="429") as error:
            market.fetch_snapshot(settings, ["AAA"], NOW, client=client)
        assert error.value.retry_at == NOW + timedelta(seconds=120)
        with pytest.raises(market.MarketDataError, match="429"):
            market.fetch_snapshot(settings, ["AAA"], NOW + timedelta(seconds=15), client=client)
    assert len(calls) == 1
    assert "fictional-secret" not in str(error.value)


def test_batch_partial_response_and_regular_session_only(settings):
    requests = []

    def respond(request):
        requests.append(request)
        assert request.method == "GET"
        if request.url.path == "/v2/calendar":
            return httpx.Response(200, json=CALENDAR)
        assert request.url.params["symbols"] == "AAA,BBB,SPY"
        assert request.url.params["feed"] == "iex"
        if request.url.path.endswith("latest"):
            return httpx.Response(
                200,
                json={"trades": {"AAA": {"p": 99, "t": "2026-11-27T15:59:50Z"}}},
            )
        if request.url.params["timeframe"] == "1Min":
            return httpx.Response(
                200,
                json={
                    "bars": {
                        "AAA": [
                            {"c": 98, "t": "2026-11-27T14:00:00Z"},
                            {"c": 99, "t": "2026-11-27T14:30:00Z"},
                        ],
                        "SPY": [{"c": 500, "t": "2026-11-27T14:30:00Z"}],
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "bars": {
                    "AAA": [{"c": 100, "t": "2026-11-25T05:00:00Z"}],
                    "SPY": [{"c": 499, "t": "2026-11-25T05:00:00Z"}],
                }
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        snapshot = market.fetch_snapshot(settings, ["BBB", "AAA"], NOW, client=client)
    assert snapshot.quotes["AAA"].price == 99
    assert "BBB" not in snapshot.quotes
    assert "partielle" in snapshot.errors["BBB"]
    assert snapshot.closes["AAA"].price == 100
    assert snapshot.previous_closes["AAA"] == 100
    assert len(snapshot.intraday["AAA"]) == 1
    assert len(requests) == 4  # one batch for each endpoint, not one per symbol


def test_daily_close_is_used_after_early_close(settings):
    now = datetime(2026, 11, 28, 16, tzinfo=UTC)

    def respond(request):
        if request.url.path == "/v2/calendar":
            return httpx.Response(200, json=CALENDAR)
        if request.url.path.endswith("latest"):
            return httpx.Response(200, json={"trades": {}})
        if request.url.params["timeframe"] == "1Min":
            return httpx.Response(200, json={"bars": {}})
        return httpx.Response(
            200,
            json={
                "bars": {
                    "AAA": [
                        {"c": 101, "t": "2026-11-25T05:00:00Z"},
                        {"c": 99, "t": "2026-11-27T05:00:00Z"},
                    ]
                }
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        snapshot = market.fetch_snapshot(settings, ["AAA"], now, client=client)
    assert snapshot.closes["AAA"].price == 99
    assert snapshot.closes["AAA"].timestamp == datetime(2026, 11, 27, 18, tzinfo=UTC)
    assert snapshot.previous_closes["AAA"] == 101


def test_network_failure_is_explicit(settings):
    def respond(request):
        raise httpx.ConnectError("fictional-secret", request=request)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(market.MarketDataError, match="échec réseau") as error:
            market.fetch_snapshot(settings, ["AAA"], NOW, client=client)
    assert "fictional-secret" not in str(error.value)


def test_minute_pagination_includes_later_symbols(settings):
    def respond(request):
        if request.url.path == "/v2/calendar":
            return httpx.Response(200, json=CALENDAR)
        if request.url.path.endswith("latest"):
            return httpx.Response(200, json={"trades": {}})
        if request.url.params["timeframe"] == "1Min":
            second = request.url.params.get("page_token") == "page-2"
            return httpx.Response(
                200,
                json={
                    "bars": {"SPY" if second else "AAA": [{"c": 100, "t": "2026-11-27T14:30:00Z"}]},
                    "next_page_token": None if second else "page-2",
                },
            )
        return httpx.Response(200, json={"bars": {}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        snapshot = market.fetch_snapshot(settings, ["AAA"], NOW, client=client)
    assert snapshot.intraday["SPY"]
    assert snapshot.intraday["AAA"]
