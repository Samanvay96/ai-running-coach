"""Tests for the morning brief gating logic in src/prerun.py.

The gates that matter:
  1. Only fire at runner-local TARGET_LOCAL_HOUR (06:00 by default).
  2. Only fire on days that resolve to a run — the day's own prescribed slot,
     or one carried over from a nearby day and still outstanding this week.
     A future slot pulled forward onto a rest day does NOT count.
  3. Don't fire twice for the same runner-local date.
"""

import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.db import Database
from src.prerun import TARGET_LOCAL_HOUR, run_prerun
from src.training_plan import PrescribedRun, ResolvedRun


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


def _patch_plan(workout_type: str, *, shifted: bool = False, pulled_forward: bool = False):
    """Patch TrainingPlan so resolution returns a deterministic slot.

    workout_type "rest" means nothing resolves for today — the gate's real input
    is whether resolve_run_for_date found a free slot at all, not the slot's
    type. get_week_for_date returns None so prerun skips the DB lookup for
    already-completed dates; the resolution itself is what's under test here.

    `shifted` carries a slot over from an earlier day (a missed run caught up
    on). `pulled_forward` instead moves a future slot early onto today.
    """
    plan_inst = MagicMock()
    plan_inst.get_week_for_date.return_value = None
    if workout_type == "rest":
        plan_inst.resolve_run_for_date.return_value = None
    else:
        run = PrescribedRun(
            workout_type=workout_type,
            distance_km=6.0,
            target_pace="6:15/km",
            description="easy 6km @ 6:15",
        )
        query = date(2026, 5, 14)
        if pulled_forward:
            prescribed_date = query + timedelta(days=1)
        elif shifted:
            prescribed_date = query - timedelta(days=1)
        else:
            prescribed_date = query
        plan_inst.resolve_run_for_date.return_value = ResolvedRun(
            run=run,
            prescribed_date=prescribed_date,
            query_date=query,
        )
    return patch("src.prerun.TrainingPlan", return_value=plan_inst)


def _patch_db_path(tmp_path: Path):
    return patch("src.prerun.DB_PATH", tmp_path / "prerun.db")


def test_gate_runner_local_hour(patched_env, tmp_path, monkeypatch):
    """Wrong runner-local hour → don't send, even if everything else passes."""
    wrong_hour = (TARGET_LOCAL_HOUR + 4) % 24
    fake_now = datetime(2026, 5, 14, wrong_hour, 30, tzinfo=timezone.utc)
    monkeypatch.setattr("src.prerun.runner_local_now", lambda db: fake_now)
    with _patch_db_path(tmp_path), _patch_plan("easy"), _patch_coach_returning("ok"):
        result = run_prerun()
    assert result == 0
    assert patched_env == []


def test_gate_rest_day(patched_env, tmp_path, monkeypatch):
    """Right hour but rest day → don't send."""
    fake_now = datetime(2026, 5, 14, TARGET_LOCAL_HOUR, 5, tzinfo=timezone.utc)
    monkeypatch.setattr("src.prerun.runner_local_now", lambda db: fake_now)
    with _patch_db_path(tmp_path), _patch_plan("rest"), _patch_coach_returning("ok"):
        result = run_prerun()
    assert result == 0
    assert patched_env == []


def test_gate_dedup_within_same_runner_date(patched_env, tmp_path, monkeypatch):
    """Second fire on the same runner-local date → don't re-send."""
    fake_now = datetime(2026, 5, 14, TARGET_LOCAL_HOUR, 5, tzinfo=timezone.utc)
    monkeypatch.setattr("src.prerun.runner_local_now", lambda db: fake_now)
    with _patch_db_path(tmp_path), _patch_plan("easy"), _patch_coach_returning("brief content"):
        first = run_prerun()
        second = run_prerun()
    assert first == 1
    assert second == 0
    assert len(patched_env) == 1


def test_happy_path_sends_brief(patched_env, tmp_path, monkeypatch):
    """All gates pass → send the brief, record it for dedup."""
    fake_now = datetime(2026, 5, 14, TARGET_LOCAL_HOUR, 5, tzinfo=timezone.utc)
    monkeypatch.setattr("src.prerun.runner_local_now", lambda db: fake_now)
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
    monkeypatch.setattr("src.prerun.runner_local_now", lambda db: fake_now)
    with _patch_db_path(tmp_path), _patch_plan("easy"), _patch_coach_returning("forced brief"):
        result = run_prerun(force=True)
    assert result == 1


def test_carried_over_run_still_gets_a_brief(patched_env, tmp_path, monkeypatch):
    """A rest day holding an outstanding slot resolves, so the brief still fires.

    Sunday is rest on paper, but when Saturday's long run hasn't been done the
    runner is very likely doing it today — sending nothing was the old gap.
    """
    fake_now = datetime(2026, 5, 14, TARGET_LOCAL_HOUR, 5, tzinfo=timezone.utc)
    monkeypatch.setattr("src.prerun.runner_local_now", lambda db: fake_now)
    with _patch_db_path(tmp_path), _patch_plan("long", shifted=True), \
            _patch_coach_returning("Stick with plan."):
        sent = run_prerun()
    assert sent == 1
    # The header has to say the run moved, or the brief reads as a bonus session.
    assert "carried over from" in patched_env[0]


def test_gate_pulled_forward_run_does_not_get_a_brief(patched_env, tmp_path, monkeypatch):
    """A rest day whose nearest outstanding slot is a future day → don't send.

    Pulling a run forward onto a rest day is a same-day call the runner makes
    in chat, not something to brief them on before they've said they're
    running — unlike a carry-over, which catches up on an already-missed run.
    """
    fake_now = datetime(2026, 5, 14, TARGET_LOCAL_HOUR, 5, tzinfo=timezone.utc)
    monkeypatch.setattr("src.prerun.runner_local_now", lambda db: fake_now)
    with _patch_db_path(tmp_path), _patch_plan("long", pulled_forward=True), \
            _patch_coach_returning("Stick with plan."):
        sent = run_prerun()
    assert sent == 0
    assert patched_env == []


def test_brief_header_omits_shift_note_on_the_prescribed_day(patched_env, tmp_path, monkeypatch):
    fake_now = datetime(2026, 5, 14, TARGET_LOCAL_HOUR, 5, tzinfo=timezone.utc)
    monkeypatch.setattr("src.prerun.runner_local_now", lambda db: fake_now)
    with _patch_db_path(tmp_path), _patch_plan("easy"), _patch_coach_returning("ok"):
        run_prerun()
    assert "carried over" not in patched_env[0]
    assert "pulled forward" not in patched_env[0]


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
