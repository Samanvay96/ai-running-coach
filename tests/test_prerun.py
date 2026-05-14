"""Tests for the morning brief gating logic in src/prerun.py.

The gates that matter:
  1. Only fire at runner-local TARGET_LOCAL_HOUR (06:00 by default).
  2. Only fire on prescribed-run days.
  3. Don't fire twice for the same runner-local date.
"""

import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.db import Database
from src.prerun import TARGET_LOCAL_HOUR, run_prerun


@pytest.fixture
def patched_env(monkeypatch):
    """Patch external dependencies so run_prerun is testable without touching prod."""
    sent = []
    monkeypatch.setattr("src.prerun.send_coaching_message", lambda msg: sent.append(msg))
    monkeypatch.setattr("src.prerun.send_error_alert", lambda msg: None)
    return sent


def _patch_coach_returning(text: str):
    """Patch Coach so morning_brief returns a fixed string without hitting the API."""
    instance = MagicMock()
    instance.morning_brief.return_value = text
    return patch("src.prerun.Coach", return_value=instance)


def _patch_plan(workout_type: str):
    """Patch TrainingPlan so get_prescribed_run returns a deterministic workout type."""
    plan_inst = MagicMock()
    prescribed = MagicMock()
    prescribed.workout_type = workout_type
    prescribed.description = "easy 6km @ 6:15"
    plan_inst.get_prescribed_run.return_value = prescribed
    return patch("src.prerun.TrainingPlan", return_value=plan_inst)


def _patch_db_path(tmp_path: Path):
    return patch("src.prerun.DB_PATH", tmp_path / "prerun.db")


def test_gate_runner_local_hour(patched_env, tmp_path, monkeypatch):
    """Wrong runner-local hour → don't send, even if everything else passes."""
    wrong_hour = (TARGET_LOCAL_HOUR + 4) % 24
    fake_now = datetime(2026, 5, 14, wrong_hour, 30, tzinfo=timezone.utc)
    monkeypatch.setattr("src.prerun._runner_local_now", lambda db: fake_now)
    with _patch_db_path(tmp_path), _patch_plan("easy"), _patch_coach_returning("ok"):
        result = run_prerun()
    assert result == 0
    assert patched_env == []


def test_gate_rest_day(patched_env, tmp_path, monkeypatch):
    """Right hour but rest day → don't send."""
    fake_now = datetime(2026, 5, 14, TARGET_LOCAL_HOUR, 5, tzinfo=timezone.utc)
    monkeypatch.setattr("src.prerun._runner_local_now", lambda db: fake_now)
    with _patch_db_path(tmp_path), _patch_plan("rest"), _patch_coach_returning("ok"):
        result = run_prerun()
    assert result == 0
    assert patched_env == []


def test_gate_dedup_within_same_runner_date(patched_env, tmp_path, monkeypatch):
    """Second fire on the same runner-local date → don't re-send."""
    fake_now = datetime(2026, 5, 14, TARGET_LOCAL_HOUR, 5, tzinfo=timezone.utc)
    monkeypatch.setattr("src.prerun._runner_local_now", lambda db: fake_now)
    with _patch_db_path(tmp_path), _patch_plan("easy"), _patch_coach_returning("brief content"):
        first = run_prerun()
        second = run_prerun()
    assert first == 1
    assert second == 0
    assert len(patched_env) == 1


def test_happy_path_sends_brief(patched_env, tmp_path, monkeypatch):
    """All gates pass → send the brief, record it for dedup."""
    fake_now = datetime(2026, 5, 14, TARGET_LOCAL_HOUR, 5, tzinfo=timezone.utc)
    monkeypatch.setattr("src.prerun._runner_local_now", lambda db: fake_now)
    with _patch_db_path(tmp_path), _patch_plan("easy"), _patch_coach_returning("Stick with plan. Easy 6km looks right."):
        sent = run_prerun()
    assert sent == 1
    assert len(patched_env) == 1
    # Message must be visually distinguished from post-run analyses
    assert "Morning brief" in patched_env[0]


def test_force_flag_bypasses_hour_gate(patched_env, tmp_path, monkeypatch):
    """force=True (CLI --force) ignores the hour gate so we can re-trigger manually."""
    wrong_hour = (TARGET_LOCAL_HOUR + 8) % 24
    fake_now = datetime(2026, 5, 14, wrong_hour, 30, tzinfo=timezone.utc)
    monkeypatch.setattr("src.prerun._runner_local_now", lambda db: fake_now)
    with _patch_db_path(tmp_path), _patch_plan("easy"), _patch_coach_returning("forced brief"):
        result = run_prerun(force=True)
    assert result == 1


# ---------------- prerun_sent table ----------------


def test_prerun_sent_today_round_trips():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "p.db")
        d = "2026-05-14"
        assert not db.prerun_sent_today(d)
        db.save_prerun(d, "stick with plan")
        assert db.prerun_sent_today(d)
        # Different date isn't dedup'd
        assert not db.prerun_sent_today("2026-05-15")
        db.close()
