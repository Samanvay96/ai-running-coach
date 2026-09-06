"""Tests for src/dashboard.py's data-assembly logic, plus a handful of
render-output assertions for bugs that were real (found from a live phone
screenshot, not hypothetical): a shifted run misclassified in the zone-trend
chart, a table that overflowed instead of scrolling, cadence baselines
leaking future runs into a past run's comparison. The general rule stays
that HTML/SVG templates aren't tested byte-for-byte — these assertions target
the specific thing that broke, not the surrounding markup.
"""

import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from src.dashboard import (
    _acr_status,
    _ago_string,
    _longest_run_so_far,
    _render_easy_trend,
    _render_full_plan,
    _render_recent_runs,
    _render_this_week,
    _render_zone_trend,
    _svg_zone_trend,
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


# ---------------- _svg_zone_trend ----------------


def test_zone_trend_chart_has_a_fixed_axis_and_target_reference():
    """The original bug: a plain sparkline autoscaled its y-axis to just the
    shown points, so the reader had no way to tell a real dip from noise on a
    metric (a %) that has a natural 0-100 domain and an explicit 80% target."""
    points = [
        {"date": "2026-08-01", "pct": 95.0, "workout_type": "easy"},
        {"date": "2026-08-03", "pct": 68.0, "workout_type": "long"},
        {"date": "2026-08-05", "pct": 90.0, "workout_type": "easy"},
    ]
    svg = _svg_zone_trend(points)
    assert ">0<" in svg and ">50<" in svg and ">100<" in svg  # fixed axis labels present
    assert "80% target" in svg
    assert 'class="mk-long"' in svg   # the long run gets a shape-coded marker
    assert 'class="mk-easy"' in svg


def test_zone_trend_chart_shows_empty_state_below_two_points():
    assert "Not enough data" in _svg_zone_trend([{"date": "2026-08-01", "pct": 90.0, "workout_type": "easy"}])


# ---------------- _render_zone_trend: shift-aware classification ----------------


def _save_run_with_zones(db: Database, activity_id: int, day: str, easy_pct_zones: list[int]) -> None:
    """easy_pct_zones: [secs_in_z1, secs_in_z2, secs_in_z3plus]."""
    import json
    hr_zones_json = json.dumps([
        {"zoneNumber": 1, "secsInZone": easy_pct_zones[0]},
        {"zoneNumber": 2, "secsInZone": easy_pct_zones[1]},
        {"zoneNumber": 3, "secsInZone": easy_pct_zones[2]},
    ])
    db.save_activity(
        activity_id=activity_id, start_time=f"{day} 08:00:00", activity_type="running",
        distance_km=10.0, duration_seconds=4000, avg_pace="6:40",
        avg_hr=145, max_hr=160, calories=600, aerobic_te=3.0,
        vo2max=None, raw_json="{}", splits_json="[]", hr_zones_json=hr_zones_json,
    )


def test_zone_trend_classifies_a_shifted_long_run_as_long_not_other(two_week_env):
    """Regression: get_prescribed_run is exact-date only, so a long run done
    two days late used to fall through to "other" — inconsistent with the
    Recent Runs table below it, which already resolves shifts correctly."""
    plan, db = two_week_env
    db.save_hr_zones(
        fetched_date="2026-03-01", sport="RUNNING", training_method="HR_RESERVE",
        max_hr=190, resting_hr=45, floors={1: 100, 2: 137, 3: 153, 4: 166, 5: 180},
        raw_json="{}",
    )
    _save_run_with_zones(db, 1, "2026-03-03", [0, 850, 150])   # Tue week 1, its own slot
    _save_run_with_zones(db, 2, "2026-03-09", [0, 680, 320])   # Mon — week 1's Sat long run, 2 days late
    _save_run_with_zones(db, 3, "2026-03-10", [0, 900, 100])   # Tue week 2, its own slot — blocks the
    # nearer 1-day candidate so 03-09 has nothing closer to resolve to than Saturday's long run (2 days)
    html = _render_zone_trend(plan, db)
    assert "2026-03-09 (long)" in html
    assert "2026-03-09 (other)" not in html


# ---------------- table/layout fixes (mobile: cut-off / illegibly small) ----------------


def test_easy_trend_table_is_wrapped_for_horizontal_scroll(two_week_env):
    plan, db = two_week_env
    _save_run(db, 1, "2026-03-03", km=5.0)
    _save_run(db, 2, "2026-03-10", km=5.0)
    recent = db.get_recent_activities(limit=10)
    html = _render_easy_trend(plan, recent)
    if "<table>" in html:  # only meaningful once there are >=2 easy runs to trend
        assert '<div class="table-scroll">' in html


def test_full_plan_wraps_instead_of_forcing_a_wide_unreadable_table(two_week_env):
    """A <table> here forces every row onto one line (white-space: nowrap,
    shared with every other table on the page) — fine for short numeric
    cells, but the free-text "Sessions" column made rows so wide the page
    either cut them off or shrank the whole table to an illegible size on a
    phone. A wrapping block list sidesteps the problem."""
    plan, _ = two_week_env
    html = _render_full_plan(plan)
    assert "<table" not in html
    assert 'class="plan-weeks"' in html
    assert "Wk 1" in html and "Wk 2" in html


def test_this_week_grid_shows_pace_and_a_hover_title(two_week_env):
    """The pace band is shown inline for a glance, and the full pace_brief()
    (which can carry an MP-finish segment too long for the cell) is on the
    title attribute for hover/long-press."""
    plan, db = two_week_env
    week = plan.get_week_for_date(date(2026, 3, 2))
    html = _render_this_week(plan, db, week, date(2026, 3, 3))
    assert "day-pace" in html
    assert 'title="' in html
    assert re.search(r"\d:\d\d", html)  # some pace text made it into the cell


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
