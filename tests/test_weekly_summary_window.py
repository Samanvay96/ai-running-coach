"""When the weekly review fires.

Regression: the summary triggered on the first Sunday poll after midnight, in
the *Pi's* timezone. A long run moved onto Sunday was therefore reviewed hours
before it happened — the week read short by that run, and the run itself landed
in no summary at all, since the next window covers the following Monday onward.

It now waits for 23:00 runner-local on Sunday, with a Monday catch-up so a poll
missed at 23:00 (poller down, Garmin erroring) doesn't drop the week entirely.
Dedup on week_start keeps it to one send across both windows.

Reference dates: Sun 2026-03-08, Mon 2026-03-09, week starts Mon 2026-03-02.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from src.poller import WEEKLY_SUMMARY_LOCAL_HOUR, weekly_summary_window

SUNDAY = date(2026, 3, 8)
MONDAY_AFTER = date(2026, 3, 9)
WEEK_START = date(2026, 3, 2)


def _at(d: date, hour: int) -> datetime:
    return datetime(d.year, d.month, d.day, hour, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize("hour", [0, 6, 12, 22])
def test_sunday_before_the_hour_is_not_due(hour):
    """The old behaviour — firing early on Sunday — must not come back."""
    assert weekly_summary_window(_at(SUNDAY, hour)) is None


def test_sunday_night_covers_monday_through_sunday():
    assert weekly_summary_window(_at(SUNDAY, WEEKLY_SUMMARY_LOCAL_HOUR)) == (WEEK_START, SUNDAY)


def test_late_sunday_still_fires():
    """Any hour at or past the target, not just the target hour exactly — the
    poll that lands in the 23:00 slot may be a few minutes either side."""
    assert weekly_summary_window(_at(SUNDAY, 23)) == (WEEK_START, SUNDAY)


@pytest.mark.parametrize("hour", [0, 9, 23])
def test_monday_is_a_catch_up_for_the_week_just_ended(hour):
    assert weekly_summary_window(_at(MONDAY_AFTER, hour)) == (WEEK_START, SUNDAY)


@pytest.mark.parametrize("offset", [1, 2, 3, 4, 5])  # Tue–Sat
def test_midweek_is_never_due(offset):
    day = WEEK_START + timedelta(days=offset)
    for hour in (0, 12, 23):
        assert weekly_summary_window(_at(day, hour)) is None


def test_sunday_and_monday_windows_agree():
    """Both paths must resolve to the same week, or the Monday catch-up would
    send a second summary instead of being deduped by week_start."""
    assert weekly_summary_window(_at(SUNDAY, 23)) == weekly_summary_window(_at(MONDAY_AFTER, 8))
