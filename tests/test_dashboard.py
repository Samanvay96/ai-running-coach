"""Tests for src/dashboard.py's data-assembly logic.

The HTML/SVG string templates aren't tested directly (pure rendering, no
branching worth asserting byte-for-byte — consistent with this repo not
testing format_recovery's exact prose either). What's tested is the one
genuinely new piece of logic, _weekly_volume_series, plus the small pure
helpers and an end-to-end smoke test that the whole pipeline doesn't crash
and produces a well-formed page.
"""

from datetime import date, timedelta
from pathlib import Path

import pytest

from src.dashboard import (
    _acr_status,
    _ago_string,
    _longest_run_so_far,
    _render_recent_runs,
    _weekly_volume_series,
    run_dashboard,
)
from src.db import Database
from src.training_plan import TrainingPlan
from test_training_plan import _write_two_week_workbook


def _save_run(db: Database, activity_id: int, day: str, km: float = 20.0) -> None:
    db.save_activity(
        activity_id=activity_id, start_time=f"{day} 08:00:00", activity_type="running",
        distance_km=km, duration_seconds=int(km * 400), avg_pace="6:40",
        avg_hr=145, max_hr=160, calories=int(km * 60), aerobic_te=3.0,
        vo2max=None, raw_json="{}", splits_json="[]",
    )


@pytest.fixture
def two_week_env(tmp_path):
    """Two consecutive plan weeks (Tue 5km + Sat 20km / 22km), for shift and
    week-boundary cases. Mon 2026-03-02..Sun 03-08 then Mon 03-09..Sun 03-15."""
    plan = TrainingPlan(str(_write_two_week_workbook(tmp_path / "two_week.xlsx")))
    db = Database(tmp_path / "two_week.db")
    yield plan, db
    db.close()


# ---------------- _weekly_volume_series ----------------


def test_volume_series_credits_a_same_week_shift_to_its_own_week(two_week_env):
    """Saturday's long run done on Sunday still counts for Saturday's week."""
    plan, db = two_week_env
    _save_run(db, 1, "2026-03-08", km=20.0)  # Sun, week 1 — Sat's slot, shifted 1 day
    series = _weekly_volume_series(plan, db, date(2026, 3, 2), date(2026, 3, 15))
    by_week = {s["week_number"]: s for s in series}
    assert by_week[1]["actual_km"] == 20.0
    assert by_week[1]["extra_km"] == 0.0
    assert by_week[2]["actual_km"] == 0.0


def test_volume_series_credits_a_cross_week_boundary_shift_correctly(two_week_env):
    """The de88740 case: a Saturday long run done the following Monday crosses
    into the next plan week and must still credit the ORIGINAL week, not the
    week the activity's calendar date falls in."""
    plan, db = two_week_env
    _save_run(db, 1, "2026-03-09", km=20.0)  # Mon, week 2 — week 1's delayed long run
    _save_run(db, 2, "2026-03-10", km=5.0)   # Tue, week 2 — its own slot (blocks the nearer candidate)
    series = _weekly_volume_series(plan, db, date(2026, 3, 2), date(2026, 3, 15))
    by_week = {s["week_number"]: s for s in series}
    assert by_week[1]["actual_km"] == 20.0   # credited back to week 1, not week 2
    assert by_week[1]["extra_km"] == 0.0
    assert by_week[2]["actual_km"] == 5.0    # only its own Tuesday run
    assert by_week[2]["extra_km"] == 0.0


def test_volume_series_tags_an_unmatched_run_as_extra_on_its_own_week(two_week_env):
    """A run with no nearby free slot (too far to be a shift) counts toward its
    own calendar week and is tagged extra_km, distinct from prescribed volume."""
    plan, db = two_week_env
    _save_run(db, 1, "2026-03-03", km=5.0)   # Tue, week 1 — its own slot, claims it
    _save_run(db, 2, "2026-03-04", km=8.0)   # Wed, week 1 — bonus run, no slot within reach
    series = _weekly_volume_series(plan, db, date(2026, 3, 2), date(2026, 3, 15))
    by_week = {s["week_number"]: s for s in series}
    assert by_week[1]["actual_km"] == 13.0   # 5 (prescribed) + 8 (extra)
    assert by_week[1]["extra_km"] == 8.0


def test_volume_series_empty_db_is_all_zero_not_an_exception(two_week_env):
    plan, db = two_week_env
    series = _weekly_volume_series(plan, db, date(2026, 3, 2), date(2026, 3, 15))
    assert len(series) == 2
    assert all(s["actual_km"] == 0.0 and s["extra_km"] == 0.0 for s in series)
    assert [s["target_km"] for s in series] == [25.0, 27.0]


def test_volume_series_returns_empty_list_outside_the_plan_window(two_week_env):
    plan, db = two_week_env
    assert _weekly_volume_series(plan, db, date(2020, 1, 1), date(2020, 1, 31)) == []


# ---------------- _longest_run_so_far ----------------


def test_longest_run_so_far_is_none_on_empty_db(two_week_env):
    _, db = two_week_env
    assert _longest_run_so_far(db, date(2026, 3, 2), date(2026, 3, 15)) is None


def test_longest_run_so_far_picks_the_true_max(two_week_env):
    _, db = two_week_env
    _save_run(db, 1, "2026-03-03", km=5.0)
    _save_run(db, 2, "2026-03-07", km=20.0)
    _save_run(db, 3, "2026-03-10", km=12.0)
    assert _longest_run_so_far(db, date(2026, 3, 2), date(2026, 3, 15)) == 20.0


# ---------------- pure threshold helpers ----------------


@pytest.mark.parametrize("ratio,label", [
    (None, "No data"),
    (0.5, "Low"),
    (0.8, "Sweet spot"),
    (1.3, "Sweet spot"),
    (1.4, "Elevated"),
    (1.64, "High"),
])
def test_acr_status_labels(ratio, label):
    assert _acr_status(ratio)[0] == label


def test_ago_string_never_when_no_poll_recorded():
    s, stale = _ago_string(None)
    assert s == "never"
    assert stale is True


def test_ago_string_stale_past_ninety_minutes():
    from datetime import datetime, timezone
    old = datetime.now(timezone.utc) - timedelta(hours=3)
    s, stale = _ago_string(old)
    assert stale is True
    assert "h" in s


def test_ago_string_fresh_within_the_hour():
    from datetime import datetime, timezone
    recent = datetime.now(timezone.utc) - timedelta(minutes=10)
    s, stale = _ago_string(recent)
    assert stale is False
    assert "min ago" in s


# ---------------- _render_recent_runs ----------------


def test_recent_runs_cadence_baseline_only_uses_strictly_prior_runs(two_week_env):
    """compute_cadence_context takes its baseline from list order, not by date
    — passing it anything but "the runs before this one" leaks a later run's
    cadence into an earlier row's baseline. recent_runs here is DESC (newest
    first): the newest row's baseline (200, 190) excludes only itself; the
    middle row's baseline must be (180,) alone, NOT (200, 180)."""
    plan, _ = two_week_env
    recent = [
        {"start_time": "2026-03-10 08:00:00", "distance_km": 5.0, "avg_pace_min_km": "6:00",
         "avg_hr": 140, "avg_cadence": 210.0, "splits_json": "[]"},
        {"start_time": "2026-03-08 08:00:00", "distance_km": 20.0, "avg_pace_min_km": "7:00",
         "avg_hr": 145, "avg_cadence": 180.0, "splits_json": "[]"},
        {"start_time": "2026-03-03 08:00:00", "distance_km": 5.0, "avg_pace_min_km": "6:00",
         "avg_hr": 140, "avg_cadence": 200.0, "splits_json": "[]"},
    ]
    html = _render_recent_runs(plan, recent)
    # newest row (210 spm): baseline is the mean of (180, 200) = 190 -> delta +20
    assert "210 (+20)" in html
    # middle row (180 spm): baseline must be ONLY the strictly-older row (200),
    # not the newer 210 leaking in -> delta -20, not the wrong -15
    assert "180 (-20)" in html
    assert "180 (-15)" not in html


# ---------------- run_dashboard() end-to-end smoke test ----------------


class _FrozenDate(date):
    """A real `date` subclass whose .today() is pinned, so run_dashboard()'s
    internal `date.today()` lands inside the synthetic plan's window without
    touching how coach.py/training_plan.py use their own `date` imports —
    they all take `today` as an explicit parameter already."""

    _frozen = date(2026, 3, 10)  # Tuesday, week 2

    @classmethod
    def today(cls):
        return cls._frozen


def test_run_dashboard_end_to_end_smoke(two_week_env, tmp_path, monkeypatch):
    plan, db = two_week_env
    plan_path = tmp_path / "two_week.xlsx"  # already written by the fixture
    _save_run(db, 1, "2026-03-03", km=5.0)
    _save_run(db, 2, "2026-03-07", km=20.0)
    db.close()  # run_dashboard opens its own connection

    monkeypatch.setattr("src.dashboard.date", _FrozenDate)
    monkeypatch.setattr("src.dashboard.send_error_alert", lambda msg: None)

    out_dir = tmp_path / "out"
    ok = run_dashboard(db_path=tmp_path / "two_week.db", plan_path=plan_path, output_dir=out_dir)

    assert ok is True
    html = (out_dir / "index.html").read_text()
    assert html.startswith("<!doctype html>")
    assert html.rstrip().endswith("</html>")
    assert "Week 2" in html
    assert "None" not in html


def test_run_dashboard_survives_a_render_error_and_keeps_the_prior_file(tmp_path, monkeypatch):
    """A crash mid-render must not leave a torn file, and must not raise —
    the caller (systemd) only cares about the exit code."""
    from src.db import Database as DB

    plan_path = tmp_path / "broken.xlsx"
    plan_path.write_text("not a real workbook")  # openpyxl will fail to load this
    DB(tmp_path / "empty.db").close()

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "index.html").write_text("<!doctype html>PRIOR GOOD PAGE</html>")

    alerts = []
    monkeypatch.setattr("src.dashboard.send_error_alert", lambda msg: alerts.append(msg))

    ok = run_dashboard(db_path=tmp_path / "empty.db", plan_path=plan_path, output_dir=out_dir)

    assert ok is False
    assert alerts  # an alert was sent
    assert "PRIOR GOOD PAGE" in (out_dir / "index.html").read_text()  # untouched
