from datetime import UTC, date, datetime

from finlab.ingest.calendar import is_session, last_completed_session


def test_regular_and_observed_holidays():
    for day in (
        date(2026, 1, 1),
        date(2026, 1, 19),
        date(2026, 2, 16),
        date(2026, 4, 3),
        date(2026, 5, 25),
        date(2026, 6, 19),
        date(2026, 7, 3),
        date(2026, 9, 7),
        date(2026, 11, 26),
        date(2026, 12, 25),
    ):
        assert not is_session(day)
    assert is_session(date(2026, 11, 27))
    assert is_session(date(2021, 12, 31))  # Saturday New Year's has no Friday closure.


def test_completed_session_before_and_after_evening_window():
    assert last_completed_session(datetime(2026, 11, 21, 0, tzinfo=UTC)) == date(2026, 11, 20)
    assert last_completed_session(datetime(2026, 11, 20, 20, tzinfo=UTC)) == date(2026, 11, 19)
