"""Timezone helpers for resolving the runner's local 'today'.

Garmin returns both startTimeLocal and startTimeGMT on every activity, so we
can derive the runner's UTC offset at run time without GPS coords or extra
deps. That offset is then used so 'today' in the coach prompt matches what
the runner sees on their watch — a Pi in London correctly says Sydney's
date when the runner is in Sydney.
"""
from datetime import datetime


def parse_garmin_timestamp(s: str | None) -> datetime | None:
    """Parse a Garmin local/GMT timestamp (e.g. '2026-05-14 09:23:45') into a naive datetime."""
    if not s or not isinstance(s, str):
        return None
    cleaned = s.replace("T", " ").split(".")[0].rstrip("Z")
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None


def compute_tz_offset_minutes(local_str: str | None, gmt_str: str | None) -> int | None:
    """UTC offset in minutes from a Garmin startTimeLocal / startTimeGMT pair.

    Returns None if either timestamp is missing or unparseable. Supports
    half-hour offsets (India +5:30, Nepal +5:45) — output is exact minutes.
    """
    local = parse_garmin_timestamp(local_str)
    gmt = parse_garmin_timestamp(gmt_str)
    if local is None or gmt is None:
        return None
    return int(round((local - gmt).total_seconds() / 60))


def format_utc_offset(minutes: int) -> str:
    """Format a UTC offset (in minutes) as ±HH:MM. e.g. 600 → '+10:00', -330 → '-05:30'."""
    sign = "+" if minutes >= 0 else "-"
    m = abs(minutes)
    return f"{sign}{m // 60:02d}:{m % 60:02d}"
