"""Local US equity session rules used for end-of-day synchronization.

Regular NYSE holidays; exceptional closures can be configured by callers.
Source: https://www.nyse.com/markets/hours-calendars
This calendar never invents missing prices and is not an execution calendar.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from dateutil.easter import easter

NY = ZoneInfo("America/New_York")
DAY = timedelta(days=1)


def _nth(year, month, weekday, n):
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _observed(day):
    return day - DAY if day.weekday() == 5 else day + DAY if day.weekday() == 6 else day


def is_session(day: date, extra_closures: frozenset[date] = frozenset()) -> bool:
    year = day.year
    memorial = date(year, 6, 1) - DAY
    memorial -= timedelta(days=(memorial.weekday() - 0) % 7)
    holidays = {
        date(year, 1, 1) + (DAY if date(year, 1, 1).weekday() == 6 else timedelta(0)),
        _nth(year, 2, 0, 3),
        easter(year) - 2 * DAY,
        memorial,
        _observed(date(year, 7, 4)),
        _nth(year, 9, 0, 1),
        _nth(year, 11, 3, 4),
        _observed(date(year, 12, 25)),
    }
    if year >= 1998:
        holidays.add(_nth(year, 1, 0, 3))
    if year >= 2022:
        holidays.add(_observed(date(year, 6, 19)))
    # Known one-off full closures within supported modern historical coverage.
    exceptional = {
        date(2004, 6, 11),
        date(2007, 1, 2),
        date(2012, 10, 29),
        date(2012, 10, 30),
        date(2018, 12, 5),
        date(2025, 1, 9),
    }
    exceptional.update(date(2001, 9, d) for d in range(11, 15))
    return day.weekday() < 5 and day not in holidays | exceptional | set(extra_closures)


def last_completed_session(now: datetime) -> date:
    if now.tzinfo is None:
        raise ValueError("Timezone-aware time required")
    local = now.astimezone(NY)
    day = local.date()
    # Conservative 18:00 ET publication window also covers early-close sessions.
    if local.hour < 18:
        day -= DAY
    while not is_session(day):
        day -= DAY
    return day
