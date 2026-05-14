"""Tests for the daily wellness/heartbeat alert checks in src/alerts.py."""

import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.alerts import HEARTBEAT_STALE_HOURS, check_heartbeat
from src.db import Database


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmp:
        d = Database(Path(tmp) / "alerts.db")
        yield d
        d.close()


# ---------------- check_heartbeat ----------------


def test_check_heartbeat_fires_when_poller_never_ran(db):
    """Fresh install (no completion logged) → loud alert."""
    alert = check_heartbeat(db)
    assert alert is not None
    assert alert.kind == "poller_stalled"
    assert "never completed" in alert.message


def test_check_heartbeat_quiet_when_recent(db):
    """Successful poll within the last 6h → no alert."""
    db.mark_poll_completed()
    assert check_heartbeat(db) is None


def test_check_heartbeat_fires_when_stale(db):
    """Manually backdate the last-poll timestamp to >6h ago and confirm it fires."""
    backdate = (datetime.now(timezone.utc) - timedelta(hours=HEARTBEAT_STALE_HOURS + 1))
    backdate_str = backdate.strftime("%Y-%m-%d %H:%M:%S")
    db.conn.execute(
        """INSERT INTO system_health (id, last_poll_completed_at) VALUES (1, ?)
           ON CONFLICT(id) DO UPDATE SET last_poll_completed_at = ?""",
        (backdate_str, backdate_str),
    )
    db.conn.commit()
    alert = check_heartbeat(db)
    assert alert is not None
    assert alert.kind == "poller_stalled"
    # Message includes the stale duration so the operator knows scale
    assert "hasn't completed" in alert.message


def test_heartbeat_threshold_catches_three_missed_polls():
    """Sanity: 6h threshold is exactly 3 missed 2-hourly polls."""
    assert HEARTBEAT_STALE_HOURS == 6


# ---------------- mark_poll_completed plumbing ----------------


def test_mark_poll_completed_upserts(db):
    db.mark_poll_completed()
    first = db.get_last_poll_completed_at()
    assert first is not None
    # A second call should overwrite, not error on conflict
    db.mark_poll_completed()
    second = db.get_last_poll_completed_at()
    assert second is not None and second >= first
