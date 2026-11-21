"""Read-only Alpaca IEX adapter. No order or account mutation endpoints are used.

API references: docs.alpaca.markets/us/reference/{stocklatesttrades-1,stockbars}
and /us/reference/legacycalendar. Provider errors never include response bodies,
request headers or credentials. A shared cooldown honours 429 across UI reruns.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from math import isfinite
from threading import Lock
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import httpx

from finlab.ingest.weekly_prices import download
from finlab.research.weekly import Bars

if TYPE_CHECKING:
    from collections.abc import Iterator

    from finlab.config import Settings

NY = ZoneInfo("America/New_York")
DATA_URL = "https://data.alpaca.markets/v2/stocks"
CALENDAR_URL = "https://paper-api.alpaca.markets/v2/calendar"
FEED = "iex"
CURRENCY = "USD"
_LOCK = Lock()
_COOLDOWNS: dict[str, datetime] = {}
_CALENDARS: dict[tuple[str, date], tuple[datetime, list[dict]]] = {}


class MarketDataError(ValueError):
    """A safe user-facing failure, with an optional earliest retry instant."""

    def __init__(self, message: str, *, retry_at: datetime | None = None) -> None:
        super().__init__(message)
        self.retry_at = retry_at


@dataclass(frozen=True)
class Quote:
    price: float
    timestamp: datetime

    def __post_init__(self) -> None:
        if not isfinite(self.price) or self.price <= 0 or self.timestamp.tzinfo is None:
            raise ValueError("A quote requires a positive finite price and timezone-aware time.")


@dataclass(frozen=True)
class MarketSession:
    is_open: bool
    trading_date: date
    last_close_date: date
    open_at: datetime
    close_at: datetime
    label: str


@dataclass(frozen=True)
class LiveSnapshot:
    quotes: dict[str, Quote]
    intraday: dict[str, list[dict]]
    session: MarketSession
    refreshed_at: datetime
    errors: dict[str, str] = field(default_factory=dict)
    closes: dict[str, Quote] = field(default_factory=dict)
    previous_closes: dict[str, float] = field(default_factory=dict)


def _credentials(settings: Settings) -> tuple[dict[str, str], str]:
    if not settings.alpaca_key_id or not settings.alpaca_secret_key:
        raise MarketDataError(
            "Clés Alpaca absentes : configurez FINLAB_ALPACA_KEY_ID et "
            "FINLAB_ALPACA_SECRET_KEY côté serveur (.env), puis reconnectez la source."
        )
    key = settings.alpaca_key_id.get_secret_value()
    secret = settings.alpaca_secret_key.get_secret_value()
    if not key.strip() or not secret.strip():
        raise MarketDataError("Clés Alpaca vides : renseignez les deux clés côté serveur.")
    fingerprint = hashlib.sha256((key + ":" + secret).encode()).hexdigest()
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}, fingerprint


def _utc(now: datetime | None) -> datetime:
    value = now or datetime.now(UTC)
    if value.tzinfo is None:
        raise ValueError("Pass a timezone-aware timestamp.")
    return value.astimezone(UTC)


@contextmanager
def _client(settings: Settings, client: httpx.Client | None) -> Iterator[tuple[httpx.Client, str]]:
    headers, fingerprint = _credentials(settings)
    if client is not None:
        yield client, fingerprint
    else:
        with httpx.Client(headers=headers, timeout=30) as owned:
            yield owned, fingerprint


def _retry_time(response: httpx.Response, now: datetime) -> datetime:
    retry = now + timedelta(seconds=60)
    value = response.headers.get("Retry-After", "")
    try:
        parsed = now + timedelta(seconds=float(value))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value).astimezone(UTC)
        except (TypeError, ValueError):
            parsed = retry
    reset = response.headers.get("X-RateLimit-Reset", "")
    with suppress(ValueError, OverflowError, OSError):
        parsed = max(parsed, datetime.fromtimestamp(float(reset), UTC))
    return max(retry, parsed)


def _check_cooldown(fingerprint: str, now: datetime) -> None:
    with _LOCK:
        retry_at = _COOLDOWNS.get(fingerprint)
    if retry_at and now < retry_at:
        raise MarketDataError(
            f"Alpaca 429 : limite de débit ; nouvelle tentative après {retry_at.isoformat()}.",
            retry_at=retry_at,
        )


def _get(
    client: httpx.Client, url: str, params: dict[str, str], fingerprint: str, now: datetime
) -> Any:
    _check_cooldown(fingerprint, now)
    try:
        response = client.get(url, params=params)
    except httpx.RequestError:
        raise MarketDataError("Alpaca indisponible : échec réseau ou délai dépassé.") from None
    if response.status_code == 429:
        retry_at = _retry_time(response, now)
        with _LOCK:
            _COOLDOWNS[fingerprint] = retry_at
        raise MarketDataError(
            f"Alpaca 429 : limite de débit ; nouvelle tentative après {retry_at.isoformat()}.",
            retry_at=retry_at,
        )
    if response.status_code in (401, 403):
        raise MarketDataError(
            f"Alpaca {response.status_code} : authentification refusée ou accès IEX non autorisé. "
            "Vérifiez vos clés paper Alpaca et les droits de données."
        )
    if response.is_error:
        raise MarketDataError(f"Alpaca HTTP {response.status_code} : données indisponibles.")
    try:
        return response.json()
    except ValueError:
        raise MarketDataError("Alpaca : réponse JSON invalide.") from None


def _session_bounds(record: dict) -> tuple[date, datetime, datetime]:
    day = date.fromisoformat(record["date"])
    opened = datetime.fromisoformat(f"{day}T{record['open']}").replace(tzinfo=NY)
    closed = datetime.fromisoformat(f"{day}T{record['close']}").replace(tzinfo=NY)
    return day, opened, closed


def session_from_calendar(records: list[dict], now: datetime) -> MarketSession:
    """Resolve regular US hours from official calendar, including holidays/early closes."""
    now = _utc(now)
    sessions = sorted((_session_bounds(r) for r in records), key=lambda r: r[0])
    completed = [s for s in sessions if s[2] <= now]
    if not completed:
        raise MarketDataError("Calendrier Alpaca incomplet : dernière clôture introuvable.")
    last = completed[-1]
    active = next((s for s in sessions if s[1] <= now < s[2]), None)
    selected = active or last
    if active:
        label = "Séance américaine ouverte"
        if active[2].hour < 16:
            label += " · séance écourtée"
    elif any(s[0] == now.astimezone(NY).date() and now < s[1] for s in sessions):
        label = "Préouverture · analyse de la dernière clôture"
    else:
        label = "Séance américaine fermée · analyse de la dernière clôture"
    return MarketSession(bool(active), selected[0], last[0], selected[1], selected[2], label)


def _calendar(client: httpx.Client, fingerprint: str, now: datetime, *, cache: bool) -> list[dict]:
    local_day = now.astimezone(NY).date()
    key = (fingerprint, local_day)
    with _LOCK:
        cached = _CALENDARS.get(key) if cache else None
    if cached is not None and now - cached[0] < timedelta(hours=1):
        return cached[1]
    records = _get(
        client,
        CALENDAR_URL,
        {"start": str(local_day - timedelta(days=30)), "end": str(local_day + timedelta(days=7))},
        fingerprint,
        now,
    )
    if not isinstance(records, list) or not records:
        raise MarketDataError("Alpaca : calendrier de marché absent ou invalide.")
    try:
        for record in records:
            _session_bounds(record)
    except (KeyError, TypeError, ValueError):
        raise MarketDataError("Alpaca : calendrier de marché invalide.") from None
    if cache:
        with _LOCK:
            _CALENDARS[key] = (now, records)
    return records


def _timestamp(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("Missing timezone")
    return timestamp.astimezone(UTC)


def _bars(
    client: httpx.Client,
    symbols: list[str],
    start: str,
    end: str,
    timeframe: str,
    fingerprint: str,
    now: datetime,
) -> dict[str, list[dict]]:
    params = {
        "symbols": ",".join(symbols),
        "start": start,
        "end": end,
        "timeframe": timeframe,
        "feed": FEED,
        "currency": CURRENCY,
        "adjustment": "raw",
        "limit": "10000",
        "sort": "asc",
    }
    result: dict[str, list[dict]] = {}
    tokens: set[str] = set()
    while True:
        data = _get(client, f"{DATA_URL}/bars", params, fingerprint, now)
        if not isinstance(data, dict) or not isinstance(data.get("bars"), dict):
            raise MarketDataError("Alpaca : réponse de barres absente ou invalide.")
        for symbol, records in data["bars"].items():
            if symbol in symbols and isinstance(records, list):
                result.setdefault(symbol, []).extend(records)
        token = data.get("next_page_token")
        if not token:
            return result
        if token in tokens:
            raise MarketDataError("Alpaca : pagination répétée, données incomplètes.")
        tokens.add(token)
        params["page_token"] = token


def fetch_snapshot(
    settings: Settings,
    symbols: list[str] | tuple[str, ...],
    now: datetime | None = None,
    *,
    client: httpx.Client | None = None,
) -> LiveSnapshot:
    """Batch quotes, regular-session minute bars and dated closes for one watchlist."""
    now = _utc(now)
    symbols = sorted({s.upper().strip() for s in symbols} | {"SPY"})
    errors: dict[str, str] = {}
    with _client(settings, client) as (api, fingerprint):
        _check_cooldown(fingerprint, now)
        calendar = _calendar(api, fingerprint, now, cache=client is None)
        session = session_from_calendar(calendar, now)
        trades = _get(
            api,
            f"{DATA_URL}/trades/latest",
            {"symbols": ",".join(symbols), "feed": FEED, "currency": CURRENCY},
            fingerprint,
            now,
        )
        if not isinstance(trades, dict) or not isinstance(trades.get("trades"), dict):
            raise MarketDataError("Alpaca : réponse des dernières transactions invalide.")
        quotes = {}
        for symbol in symbols:
            try:
                record = trades["trades"][symbol]
                quotes[symbol] = Quote(float(record["p"]), _timestamp(record["t"]))
            except (KeyError, TypeError, ValueError):
                errors[symbol] = (
                    "Réponse partielle Alpaca : dernière transaction absente ou invalide."
                )
        end = min(now, session.close_at)
        minute_records = _bars(
            api, symbols, session.open_at.isoformat(), end.isoformat(), "1Min", fingerprint, now
        )
        intraday: dict[str, list[dict]] = {}
        for symbol in symbols:
            values: dict[datetime, float] = {}
            for record in minute_records.get(symbol, []):
                try:
                    timestamp = _timestamp(record["t"])
                    close = float(record["c"])
                    if session.open_at <= timestamp < end and isfinite(close) and close > 0:
                        values[timestamp] = close
                except (KeyError, TypeError, ValueError):
                    errors[symbol] = "Réponse partielle Alpaca : barre intrajournalière invalide."
            intraday[symbol] = [
                {"time": t.isoformat(), "close": p} for t, p in sorted(values.items())
            ]
            if not values:
                errors[symbol] = errors.get(symbol, "Barres intrajournalières IEX absentes.")
        dates = sorted(date.fromisoformat(r["date"]) for r in calendar)
        prior_dates = [d for d in dates if d < session.trading_date]
        if not prior_dates:
            raise MarketDataError("Calendrier Alpaca incomplet : clôture précédente absente.")
        previous_date = prior_dates[-1]
        daily_records = _bars(
            api,
            symbols,
            previous_date.isoformat(),
            (session.last_close_date + timedelta(days=1)).isoformat(),
            "1Day",
            fingerprint,
            now,
        )
        closes = {}
        previous_closes = {}
        close_times = {d: closed for d, _, closed in map(_session_bounds, calendar)}
        for symbol in symbols:
            for record in daily_records.get(symbol, []):
                try:
                    day = _timestamp(record["t"]).astimezone(NY).date()
                    close = float(record["c"])
                    quote = Quote(close, close_times[day])
                    if day == session.last_close_date:
                        closes[symbol] = quote
                    if day == previous_date:
                        previous_closes[symbol] = close
                except (KeyError, TypeError, ValueError):
                    errors[symbol] = "Réponse partielle Alpaca : clôture quotidienne invalide."
            if symbol not in closes:
                errors[symbol] = "Réponse partielle Alpaca : dernière clôture datée absente."
            if symbol not in previous_closes:
                errors[symbol] = errors.get(
                    symbol, "Clôture précédente absente : variation inconnue."
                )
    return LiveSnapshot(quotes, intraday, session, now, errors, closes, previous_closes)


def history(
    settings: Settings,
    symbols: list[str] | tuple[str, ...],
    week: date,
    *,
    client: httpx.Client | None = None,
) -> Bars:
    """History is independently cached by the caller, never in a 15-second quote key."""
    now = datetime.now(UTC)
    with _client(settings, client) as (api, fingerprint):
        _check_cooldown(fingerprint, now)
        try:
            return download(api, sorted(set(symbols) | {"SPY"}), week)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                retry_at = _retry_time(exc.response, now)
                with _LOCK:
                    _COOLDOWNS[fingerprint] = retry_at
                raise MarketDataError(
                    f"Alpaca 429 : historique limité ; réessayez après {retry_at.isoformat()}.",
                    retry_at=retry_at,
                ) from None
            if exc.response.status_code in (401, 403):
                raise MarketDataError(
                    "Alpaca : authentification ou autorisation de données refusée."
                ) from None
            raise MarketDataError(
                f"Alpaca HTTP {exc.response.status_code} : historique indisponible."
            ) from None
        except httpx.RequestError:
            raise MarketDataError(
                "Alpaca indisponible : échec réseau pendant le chargement historique."
            ) from None
        except (KeyError, TypeError, ValueError):
            raise MarketDataError("Alpaca : historique incomplet ou invalide.") from None
