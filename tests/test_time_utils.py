"""Tests for the timezone helpers in src/time_utils.py."""

from src.time_utils import (
    compute_tz_offset_minutes,
    format_utc_offset,
    parse_garmin_timestamp,
)


# ---------------- parse_garmin_timestamp ----------------


def test_parse_garmin_timestamp_space_separator():
    """Garmin's typical activity timestamps use a space, not a T."""
    dt = parse_garmin_timestamp("2026-05-14 09:23:45")
    assert dt is not None
    assert dt.year == 2026 and dt.hour == 9 and dt.minute == 23


def test_parse_garmin_timestamp_iso_with_t():
    assert parse_garmin_timestamp("2026-05-14T09:23:45") is not None


def test_parse_garmin_timestamp_with_milliseconds():
    assert parse_garmin_timestamp("2026-05-14 09:23:45.123") is not None


def test_parse_garmin_timestamp_handles_garbage():
    assert parse_garmin_timestamp(None) is None
    assert parse_garmin_timestamp("") is None
    assert parse_garmin_timestamp("not a date") is None


# ---------------- compute_tz_offset_minutes ----------------


def test_compute_tz_offset_sydney_dst():
    """Sydney in May is UTC+10 (AEST, no DST). Local 09:30, GMT 23:30 prior day → +600."""
    offset = compute_tz_offset_minutes("2026-05-14 09:30:00", "2026-05-13 23:30:00")
    assert offset == 600


def test_compute_tz_offset_london_summer():
    """BST is UTC+1. Local 09:00, GMT 08:00 → +60."""
    assert compute_tz_offset_minutes("2026-05-14 09:00:00", "2026-05-14 08:00:00") == 60


def test_compute_tz_offset_india_half_hour():
    """IST is UTC+5:30. Local 09:30, GMT 04:00 → +330."""
    assert compute_tz_offset_minutes("2026-05-14 09:30:00", "2026-05-14 04:00:00") == 330


def test_compute_tz_offset_nepal_quarter_hour():
    """NPT is UTC+5:45 — confirms minute-precision support."""
    assert compute_tz_offset_minutes("2026-05-14 09:45:00", "2026-05-14 04:00:00") == 345


def test_compute_tz_offset_negative_west_of_utc():
    """New York EDT is UTC-4. Local 09:00, GMT 13:00 → -240."""
    assert compute_tz_offset_minutes("2026-05-14 09:00:00", "2026-05-14 13:00:00") == -240


def test_compute_tz_offset_utc_zero():
    assert compute_tz_offset_minutes("2026-05-14 09:00:00", "2026-05-14 09:00:00") == 0


def test_compute_tz_offset_handles_missing():
    assert compute_tz_offset_minutes(None, "2026-05-14 09:00:00") is None
    assert compute_tz_offset_minutes("2026-05-14 09:00:00", None) is None
    assert compute_tz_offset_minutes(None, None) is None


# ---------------- format_utc_offset ----------------


def test_format_utc_offset_positive():
    assert format_utc_offset(600) == "+10:00"


def test_format_utc_offset_negative():
    assert format_utc_offset(-240) == "-04:00"


def test_format_utc_offset_half_hour():
    assert format_utc_offset(330) == "+05:30"
    assert format_utc_offset(-330) == "-05:30"


def test_format_utc_offset_quarter_hour():
    assert format_utc_offset(345) == "+05:45"


def test_format_utc_offset_zero():
    assert format_utc_offset(0) == "+00:00"
