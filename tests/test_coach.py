"""Smoke tests for the LLM call sites in src/coach.py.

The Apr 28 silent-failure bug (`StopIteration` from `next(b.text for b in
response.content if b.type == "text")` when the model emitted only thinking
blocks) is the regression these tests are designed to catch. If you change
how `_extract_text` handles missing text blocks, run these.

Run with: `.venv/bin/python -m pytest tests/`
"""

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.coach import (
    Coach,
    _extract_text,
    _format_weekly_target,
    compute_acr,
    compute_adherence,
    compute_cadence_context,
    compute_mileage_delta,
    compute_weekly_target,
    format_cadence_context,
    compute_zone_distribution,
    format_feel,
    format_recovery,
    format_weather,
    heat_note,
    resolve_runner_today,
)
from src.config import DB_PATH, TRAINING_PLAN_PATH
from src.db import Database
from src.training_plan import TrainingPlan


def _plan_or_skip() -> TrainingPlan:
    """Load the runner's real plan, or skip.

    The workbooks hold personal training data and are gitignored, so they exist
    locally but not in a fresh clone. These tests exercise the coach against the
    live plan; without it there is nothing meaningful to assert.
    """
    if not Path(TRAINING_PLAN_PATH).exists():
        pytest.skip(f"{TRAINING_PLAN_PATH.name} not present (gitignored — personal training data)")
    return TrainingPlan(str(TRAINING_PLAN_PATH))


def _block(btype: str, text: str | None = None) -> SimpleNamespace:
    """Stand-in for an Anthropic content block with .type and .text."""
    return SimpleNamespace(type=btype, text=text or "")


def _response(blocks: list, stop_reason: str = "end_turn") -> SimpleNamespace:
    """Stand-in for an anthropic.types.Message."""
    return SimpleNamespace(
        content=blocks,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=100, output_tokens=200),
    )


def _wire_stream(client, response):
    """Wire a MagicMock client so coach's _stream_message yields `response`.

    The analyze_run / weekly_summary call sites now stream (see the 2026-06-04
    fix): `with client.messages.stream(**kw) as s: return s.get_final_message()`.
    Mirror that shape so call_args land on `.stream`, not `.create`.
    """
    ctx = client.messages.stream.return_value
    ctx.__enter__.return_value.get_final_message.return_value = response
    return client.messages.stream


# ---------------- _extract_text ----------------


def test_extract_text_returns_text_block():
    resp = _response([_block("text", "Hello, runner.")])
    assert _extract_text(resp) == "Hello, runner."


def test_extract_text_skips_thinking_block_and_returns_text():
    """Sonnet 4.6 with adaptive thinking emits both blocks; we want the text one."""
    resp = _response([
        _block("thinking", "internal reasoning"),
        _block("text", "Solid easy run."),
    ])
    assert _extract_text(resp) == "Solid easy run."


def test_extract_text_raises_when_only_thinking_block_present():
    """The Apr 28 bug: max_tokens hit during thinking, no text block emitted.

    Old code raised `StopIteration` with `str(e) == ""`, producing blank
    error logs. The new helper must raise something with stop_reason and
    block types in the message so journalctl tells us what happened.
    """
    resp = _response([_block("thinking", "ran out of tokens here")], stop_reason="max_tokens")
    with pytest.raises(RuntimeError) as exc_info:
        _extract_text(resp)
    msg = str(exc_info.value)
    # The message must surface stop_reason so the operator can act on it
    assert "max_tokens" in msg
    assert "thinking" in msg


def test_extract_text_raises_on_empty_content():
    resp = _response([])
    with pytest.raises(RuntimeError):
        _extract_text(resp)


# ---------------- analyze_run integration smoke test ----------------


@pytest.fixture
def coach():
    """Real plan + DB, mocked Anthropic client. Coach reads from DB but never
    writes during analyze_run, so this is safe against prod data."""
    plan = _plan_or_skip()
    db = Database(DB_PATH)
    c = Coach(api_key="test-key", plan=plan, db=db)
    c.client = MagicMock()
    return c


def test_analyze_run_happy_path_returns_text(coach):
    """End-to-end: analyze_run builds a prompt, streams the API, extracts text."""
    _wire_stream(coach.client, _response([
        _block("thinking", "..."),
        _block("text", "Verdict: solid run."),
    ]))
    activity = {
        "start_time": "2026-04-28 09:23:45",
        "distance_km": 4.0,
        "duration_seconds": 1670,
        "avg_pace_min_km": "6:57",
        "avg_hr": 147,
        "max_hr": 165,
        "splits_json": "[]",
        "hr_zones_json": None,
    }
    result = coach.analyze_run(activity)
    assert result == "Verdict: solid run."
    assert coach.client.messages.stream.called

    # Sanity-check the request shape that the production bug depended on
    call_kwargs = coach.client.messages.stream.call_args.kwargs
    assert call_kwargs["max_tokens"] >= 8192, (
        "max_tokens must leave headroom for adaptive thinking + text — 4096 was "
        "fully consumed by thinking on 2026-06-04, leaving no review (stop=max_tokens)"
    )
    assert call_kwargs["thinking"] == {"type": "adaptive"}, (
        "budget_tokens is rejected by the API for adaptive — see Apr 28 regression"
    )


def test_analyze_run_propagates_no_text_failure(coach):
    """If the model returns only thinking, the failure must reach the caller
    (poller's outer try/except) instead of being silently swallowed."""
    _wire_stream(
        coach.client, _response([_block("thinking", "...")], stop_reason="max_tokens")
    )
    activity = {
        "start_time": "2026-04-28 09:23:45",
        "distance_km": 4.0,
        "duration_seconds": 1670,
        "avg_pace_min_km": "6:57",
        "avg_hr": 147,
        "max_hr": 165,
        "splits_json": "[]",
        "hr_zones_json": None,
    }
    with pytest.raises(RuntimeError, match="max_tokens"):
        coach.analyze_run(activity)


# ---------------- streaming (2026-06-04 disconnect regression) ----------------
#
# The per-run analysis failed on 2026-06-04 with APIConnectionError ("Server
# disconnected without sending a response"). Root cause: the non-streaming
# request sat idle for ~180s while adaptive thinking ran, and an upstream proxy
# closed the connection. Streaming keeps the connection alive with incremental
# events. These tests lock in that the heavy thinking call sites stream and that
# the helper drains the context manager correctly.


def test_stream_message_drains_context_manager_and_returns_final():
    """_stream_message must open the stream as a context manager, return its
    final message, and exit the manager (releasing the connection)."""
    from src.coach import _stream_message

    client = MagicMock()
    sentinel = _response([_block("text", "done")])
    cm = client.messages.stream.return_value
    cm.__enter__.return_value.get_final_message.return_value = sentinel

    out = _stream_message(client, model="m", max_tokens=8192)

    assert out is sentinel
    client.messages.stream.assert_called_once_with(model="m", max_tokens=8192)
    cm.__enter__.assert_called_once()
    cm.__exit__.assert_called_once()  # connection released even on the happy path


def test_analyze_run_streams_rather_than_blocking(coach):
    """The fix: analyze_run must stream, never the blocking create() that the
    upstream idle timeout killed mid-thinking on 2026-06-04."""
    _wire_stream(coach.client, _response([
        _block("thinking", "long adaptive reasoning that used to outlive the idle timeout"),
        _block("text", "Verdict: ok."),
    ]))
    activity = {
        "start_time": "2026-06-04 10:22:21",
        "distance_km": 7.0,
        "duration_seconds": 2517,
        "avg_pace_min_km": "5:59",
        "avg_hr": 160,
        "max_hr": 183,
        "splits_json": "[]",
        "hr_zones_json": None,
    }
    result = coach.analyze_run(activity)
    assert result == "Verdict: ok."
    assert coach.client.messages.stream.called
    assert not coach.client.messages.create.called, (
        "non-streaming create() is what the upstream proxy disconnected — must stream"
    )


def test_weekly_summary_streams_with_headroom(coach):
    """weekly_summary shares analyze_run's adaptive-thinking shape, so it carries
    the same disconnect/truncation risk and must stream with the same headroom."""
    _wire_stream(coach.client, _response([
        _block("thinking", "..."),
        _block("text", "Week in review: solid."),
    ]))
    result = coach.weekly_summary("2026-06-01", "2026-06-07")
    assert result == "Week in review: solid."
    assert coach.client.messages.stream.called
    assert not coach.client.messages.create.called
    call_kwargs = coach.client.messages.stream.call_args.kwargs
    assert call_kwargs["max_tokens"] >= 8192
    assert call_kwargs["thinking"] == {"type": "adaptive"}


# ---------------- weather + RPE/feel formatting ----------------


def test_format_feel_snaps_to_garmin_buckets():
    assert format_feel(0) == "Very Weak"
    assert format_feel(25) == "Weak"
    assert format_feel(50) == "Normal"
    assert format_feel(75) == "Strong"
    assert format_feel(100) == "Very Strong"
    # Off-bucket values round to the nearest 25
    assert format_feel(60) == "Normal"
    assert format_feel(None) is None


def test_format_weather_outdoor_run():
    text = format_weather({
        "temp_c": 22, "apparent_temp_c": 24, "humidity_pct": 65,
        "wind_kph": 10, "weather_label": "Cloudy",
    })
    assert "22°C" in text
    assert "65%" in text or "65" in text
    assert "Cloudy" in text


def test_format_weather_indoor_fallback():
    assert "indoor" in format_weather({}).lower()


def test_heat_note_fires_above_25c():
    assert "heat" in heat_note({"temp_c": 28, "humidity_pct": 50}).lower()


def test_heat_note_fires_for_warm_and_humid():
    assert heat_note({"temp_c": 22, "humidity_pct": 80}) != ""


def test_heat_note_quiet_in_cool_conditions():
    assert heat_note({"temp_c": 12, "humidity_pct": 60}) == ""


# ---------------- zone distribution ----------------


def test_zone_distribution_counts_z1_and_z2_as_easy():
    """The original bug: Z2=73%, Z1=10% → coach said "missed 80% easy target".
    Both should count toward easy time."""
    zones = [
        {"zoneNumber": 1, "secsInZone": 100},
        {"zoneNumber": 2, "secsInZone": 737},
        {"zoneNumber": 3, "secsInZone": 163},
        {"zoneNumber": 4, "secsInZone": 0},
        {"zoneNumber": 5, "secsInZone": 0},
    ]
    import json as _json
    dist = compute_zone_distribution(_json.dumps(zones), None, 134, 148)
    assert dist is not None
    assert dist["z1_pct"] == 10.0
    assert dist["z2_pct"] == 73.7
    assert dist["easy_pct"] == 83.7
    assert dist["easy_pct"] >= 80, "Z1+Z2 should clear the 80% easy threshold"


def test_zone_distribution_splits_fallback_buckets_by_hr():
    """When Garmin zones aren't available, split HR is bucketed by Z2 bounds."""
    splits = [
        {"averageHR": 120, "duration": 300},  # below Z2_min → Z1
        {"averageHR": 140, "duration": 300},  # in Z2
        {"averageHR": 160, "duration": 300},  # above Z2_max → Z3+
    ]
    dist = compute_zone_distribution(None, splits, 134, 148)
    assert dist is not None
    assert dist["z1_pct"] == pytest.approx(33.3, abs=0.5)
    assert dist["z2_pct"] == pytest.approx(33.3, abs=0.5)
    assert dist["z3plus_pct"] == pytest.approx(33.3, abs=0.5)
    assert dist["easy_pct"] == pytest.approx(66.7, abs=0.5)
    assert dist["source"] == "splits_fallback"


def test_zone_distribution_handles_empty():
    assert compute_zone_distribution(None, None, 134, 148) is None
    assert compute_zone_distribution(None, [], 134, 148) is None


# ---------------- format_recovery staleness ----------------


def test_format_recovery_quiet_when_data_is_fresh():
    """Yesterday's wellness data is fresh — Garmin records sleep after waking."""
    today = date(2026, 5, 14)
    wellness = {
        "date": "2026-05-13",
        "sleep_seconds": 7 * 3600,
        "hrv_last_night": 55,
    }
    out = format_recovery(wellness, today)
    assert "stale" not in out.lower()
    assert "7.0h" in out


def test_format_recovery_flags_stale_data():
    """Wellness 3 days old should be flagged so the model discounts it."""
    today = date(2026, 5, 14)
    wellness = {
        "date": "2026-05-11",
        "sleep_seconds": 5 * 3600,
        "hrv_last_night": 40,
    }
    out = format_recovery(wellness, today)
    assert "stale" in out.lower()
    assert "3 days" in out


def test_format_recovery_without_today_skips_staleness_check():
    """Backwards compat — older callers without `today` arg don't trip the check."""
    wellness = {"date": "2026-05-01", "sleep_seconds": 7 * 3600}
    out = format_recovery(wellness)  # no today arg
    assert "stale" not in out.lower()


def test_format_recovery_handles_missing_date():
    """A wellness row without a `date` key must not crash."""
    wellness = {"sleep_seconds": 7 * 3600, "hrv_last_night": 55}
    out = format_recovery(wellness, date(2026, 5, 14))
    assert "stale" not in out.lower()


# ---------------- runner timezone resolver ----------------


def test_resolve_runner_today_uses_recent_activity_offset():
    """When a recent activity has an offset, that wins — env var ignored."""
    db = MagicMock()
    db.get_latest_tz_offset_minutes.return_value = 600  # Sydney UTC+10
    today, label = resolve_runner_today(db)
    assert "UTC+10:00" in label
    assert "auto-derived" in label
    # today should match the actual date in UTC+10
    from datetime import datetime, timedelta, timezone
    expected = datetime.now(timezone(timedelta(minutes=600))).date()
    assert today == expected


def test_resolve_runner_today_handles_half_hour_offset():
    db = MagicMock()
    db.get_latest_tz_offset_minutes.return_value = 330  # India UTC+5:30
    _, label = resolve_runner_today(db)
    assert "UTC+05:30" in label


def test_resolve_runner_today_falls_back_to_env():
    """No recent run → use the env-configured fallback."""
    db = MagicMock()
    db.get_latest_tz_offset_minutes.return_value = None
    _, label = resolve_runner_today(db)
    # The label should mention the env var fallback, regardless of what RUNNER_TIMEZONE is
    assert "env var" in label or "RUNNER_TIMEZONE unset" in label


def test_get_latest_tz_offset_only_returns_recent():
    """Activities outside the 14-day window must not bleed back in."""
    import tempfile
    from pathlib import Path as _P
    from datetime import datetime as _dt, timedelta as _td
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(_P(tmp) / "test.db")
        # An offset from 30 days ago — outside the default 14-day window
        old = (_dt.utcnow() - _td(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        db.save_activity(
            activity_id=1, start_time=old, activity_type="running",
            distance_km=5, duration_seconds=1800, avg_pace="6:00",
            avg_hr=140, max_hr=160, calories=300, aerobic_te=2.0,
            vo2max=None, raw_json="{}", splits_json="[]",
            tz_offset_minutes=60,  # London
        )
        assert db.get_latest_tz_offset_minutes(within_days=14) is None
        # Widening the window should find it
        assert db.get_latest_tz_offset_minutes(within_days=60) == 60
        db.close()


def test_update_activity_subjective_round_trips():
    """The backfill path must set rpe/feel on an existing row."""
    import tempfile
    from pathlib import Path as _P
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(_P(tmp) / "test.db")
        db.save_activity(
            activity_id=42, start_time="2026-05-14 09:30:00", activity_type="running",
            distance_km=8.0, duration_seconds=3000, avg_pace="6:15",
            avg_hr=150, max_hr=165, calories=500, aerobic_te=3.0,
            vo2max=None, raw_json="{}", splits_json="[]",
        )
        # Pre-condition: stored NULL (the bug we're recovering from)
        before = db.conn.execute("SELECT rpe, feel FROM activities WHERE activity_id=42").fetchone()
        assert before["rpe"] is None and before["feel"] is None

        db.update_activity_subjective(42, rpe=3.0, feel=75)
        after = db.conn.execute("SELECT rpe, feel FROM activities WHERE activity_id=42").fetchone()
        assert after["rpe"] == 3.0
        assert after["feel"] == 75
        db.close()


def test_backfill_tz_offsets_populates_from_raw_json():
    """Existing rows without tz_offset_minutes should pick it up from raw_json on next open."""
    import tempfile
    from pathlib import Path as _P
    with tempfile.TemporaryDirectory() as tmp:
        db_path = _P(tmp) / "test.db"
        db = Database(db_path)
        raw = (
            '{"startTimeLocal": "2026-05-14 09:30:00", '
            '"startTimeGMT": "2026-05-13 23:30:00"}'
        )
        # Insert WITHOUT tz_offset_minutes (simulates a row from before the migration)
        db.conn.execute(
            "INSERT INTO activities (activity_id, start_time, raw_json) VALUES (?, ?, ?)",
            (99, "2026-05-14 09:30:00", raw),
        )
        db.conn.commit()
        # The next backfill call should derive +600 minutes
        updated = db.backfill_tz_offsets()
        assert updated >= 1
        row = db.conn.execute(
            "SELECT tz_offset_minutes FROM activities WHERE activity_id = 99"
        ).fetchone()
        assert row["tz_offset_minutes"] == 600
        # Second call should be a no-op
        assert db.backfill_tz_offsets() == 0
        db.close()


def test_analyze_run_prompt_includes_conditions_and_subjective(coach):
    """Both new prompt sections must reach the model when fields are present."""
    _wire_stream(coach.client, _response([_block("text", "ok")]))
    activity = {
        "start_time": "2026-05-14 09:23:45",
        "distance_km": 8.0,
        "duration_seconds": 3000,
        "avg_pace_min_km": "6:15",
        "avg_hr": 152,
        "max_hr": 168,
        "splits_json": "[]",
        "hr_zones_json": None,
        "temp_c": 28,
        "humidity_pct": 60,
        "weather_label": "Sunny",
        "rpe": 7,
        "feel": 25,  # "Weak"
    }
    coach.analyze_run(activity)
    prompt = coach.client.messages.stream.call_args.kwargs["messages"][-1]["content"]
    assert "CONDITIONS:" in prompt
    assert "28°C" in prompt
    assert "Sunny" in prompt
    assert "SUBJECTIVE EFFORT" in prompt
    assert "RPE 7" in prompt
    assert "Weak" in prompt
    # Heat cue should be present for 28°C runs
    assert "heat" in prompt.lower()


def test_chat_returns_text_with_rich_context(coach):
    """chat() should call the API and return the text block."""
    coach.client.messages.create.return_value = _response([_block("text", "Sure thing.")])
    result = coach.chat("how am I tracking?")
    assert result == "Sure thing."

    # The user message should have a context prefix that includes adherence /
    # weekly target etc. (added in the Apr 28 rich-chat change).
    call_kwargs = coach.client.messages.create.call_args.kwargs
    last_user_msg = call_kwargs["messages"][-1]["content"]
    # Rich context prefix is bracketed; if it's missing, chat() lost its data.
    assert "[Context for this conversation" in last_user_msg or "Recent runs" in last_user_msg


# ---------------- weekly target framing ----------------
#
# Regression: on May 18 2026 the run-review said "16km remaining across 6 days
# (avg ~2.7km/day)". The runner doesn't run every day — the plan has rest days
# — so per-day averaging across the whole week is misleading. compute_weekly_target
# now exposes remaining *prescribed runs* and _format_weekly_target frames the
# string around that, so the model has no reason to divide by calendar days.


def test_compute_weekly_target_excludes_rest_days_from_remaining_runs():
    plan = _plan_or_skip()
    db = Database(DB_PATH)
    # 2026-05-18 is a Monday; the v5 plan schedules runs on Mon/Wed/Fri for
    # this base-bridge week, so after Monday only Wed + Fri should remain.
    t = compute_weekly_target(plan, db, date(2026, 5, 18))
    assert t is not None
    assert t["remaining_runs_count"] == 2
    weekdays = [r["weekday"] for r in t["remaining_runs"]]
    assert weekdays == ["Wed", "Fri"]
    assert t["remaining_runs_km"] == sum(r["km"] for r in t["remaining_runs"])


def test_format_weekly_target_uses_runs_not_days():
    target = {
        "week_number": 9,
        "phase": "Base (Bridge)",
        "actual_km": 6.0,
        "target_km": 22.0,
        "pct": 27.3,
        "days_remaining": 6,
        "remaining_runs_count": 2,
        "remaining_runs_km": 16.0,
        "remaining_runs": [
            {"weekday": "Wed", "type": "easy", "km": 5.0},
            {"weekday": "Fri", "type": "easy", "km": 11.0},
        ],
    }
    out = _format_weekly_target(target)
    assert "2 prescribed run(s) left" in out
    assert "16.0km total" in out
    assert "Wed easy 5.0km" in out and "Fri easy 11.0km" in out
    # The misleading framings must not appear.
    assert "days remaining" not in out
    assert "days left" not in out
    assert "/day" not in out


def test_format_weekly_target_handles_no_runs_left():
    target = {
        "week_number": 9, "phase": "Base", "actual_km": 22.0, "target_km": 22.0,
        "pct": 100.0, "days_remaining": 1, "remaining_runs_count": 0,
        "remaining_runs_km": 0.0, "remaining_runs": [],
    }
    out = _format_weekly_target(target)
    assert "No more runs scheduled" in out
    assert "days" not in out


def test_format_weekly_target_handles_outside_plan_window():
    assert _format_weekly_target(None) == "Outside training plan window"


def test_cadence_context_baselines_against_prior_runs_only():
    activity = {
        "start_time": "2026-05-20 08:04:40", "avg_cadence": 172.0,
        "ground_contact_ms": 272.8, "stride_length_cm": 87.3,
        "vertical_oscillation_cm": 7.9,
    }
    recent = [
        {"start_time": "2026-05-20 08:04:40", "avg_cadence": 172.0},  # the run itself — excluded
        {"start_time": "2026-05-18 07:30:57", "avg_cadence": 176.0},
        {"start_time": "2026-05-16 08:35:10", "avg_cadence": 178.0},
    ]
    ctx = compute_cadence_context(activity, recent)
    assert ctx["current"] == 172.0
    assert ctx["baseline"] == 177.0       # mean of 176 + 178, NOT diluted by today
    assert ctx["n_baseline"] == 2
    assert ctx["delta"] == -5.0           # down 5 spm from the runner's own usual
    assert ctx["ground_contact_ms"] == 272.8


def test_cadence_context_none_without_cadence():
    assert compute_cadence_context({"start_time": "x", "avg_cadence": None}, []) is None


def test_format_cadence_context_grounds_in_baseline_not_generic_target():
    ctx = compute_cadence_context(
        {"start_time": "d1", "avg_cadence": 172.0, "ground_contact_ms": 272.8,
         "vertical_oscillation_cm": 7.9, "stride_length_cm": 87.3},
        [{"start_time": "d0", "avg_cadence": 176.0}],
    )
    out = format_cadence_context(ctx)
    assert "your recent baseline 176.0 spm" in out
    assert "-4.0" in out
    assert "ground contact 273ms" in out
    assert "175" not in out and "178" not in out  # no stock target leaks in


def test_format_cadence_context_omits_missing_dynamics():
    ctx = compute_cadence_context({"start_time": "d1", "avg_cadence": 170.0}, [])
    out = format_cadence_context(ctx)
    assert "no prior baseline yet" in out
    assert "ground contact" not in out and "oscillation" not in out


def test_mileage_delta_uses_trailing_window_and_agrees_with_acr():
    """Mid-week, trailing-7d volume must agree in direction with ACR.

    Reproduces the 2026-05-20 artifact: a Wednesday (3 of 7 calendar days
    elapsed) with a big long run in the trailing window. The old calendar
    week-to-date comparison read volume as DOWN (only Mon+Wed banked) while
    ACR read load as UP — a contradiction the coach surfaced as a paradox.
    With matched rolling windows, volume reads up and the two never disagree.
    """
    import tempfile
    from pathlib import Path as _P
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(_P(tmp) / "test.db")
        # (date, km, load). Trailing 7d ending Wed 05-20 holds 23km incl. the
        # 05-16 long run; the prior weeks ramp up from a low post-layoff base.
        runs = [
            ("2026-04-24", 4.0, 50.0), ("2026-04-27", 4.0, 50.0),
            ("2026-05-02", 4.0, 58.5), ("2026-05-05", 5.0, 71.4),
            ("2026-05-07", 5.0, 70.2), ("2026-05-09", 10.0, 69.7),
            ("2026-05-12", 5.0, 69.4),
            ("2026-05-14", 4.0, 55.2), ("2026-05-16", 8.0, 111.8),
            ("2026-05-18", 6.0, 71.8), ("2026-05-20", 5.0, 66.7),
        ]
        for i, (d, km, load) in enumerate(runs):
            db.save_activity(
                activity_id=i + 1, start_time=f"{d} 08:00:00",
                activity_type="running", distance_km=km, duration_seconds=1800,
                avg_pace="6:30", avg_hr=145, max_hr=160, calories=300,
                aerobic_te=2.0, vo2max=None, raw_json="{}", splits_json="[]",
                training_load=load, tz_offset_minutes=60,
            )

        today = date(2026, 5, 20)  # a Wednesday — only 3/7 calendar days elapsed
        delta = compute_mileage_delta(db, today)
        acr = compute_acr(db, today)

        # Trailing 7d captures the full 23km, not the 11km calendar week-to-date.
        assert delta["last_7d_km"] == 23.0
        # Volume reads UP (was negative under the old calendar-week logic)...
        assert delta["pct_delta"] > 0
        # ...and agrees in direction with ACR — both say "ramping". The invariant
        # the fix guarantees: shared window ⇒ sign(delta) matches (ACR > 1).
        assert (delta["pct_delta"] > 0) == (acr["ratio"] > 1)
        db.close()


# ---------------- shifted run days ----------------
#
# The plan pins each session to a weekday, but a Saturday long run often gets
# done on Sunday. Before shift-tolerant resolution that run lost its
# prescription entirely (analysed as "No run prescribed") and Saturday was
# booked as a missed run, so adherence fell for a week that was actually
# completed in full.


@pytest.fixture
def shift_env(tmp_path):
    """Synthetic one-week plan + empty DB.

    The fixture week is Mon 2026-03-02 – Sun 03-08, running Tue (easy 5 km) and
    Sat (long 20 km). Built here rather than loaded from the real workbook so
    these run in a fresh clone, where the plan is gitignored.
    """
    from test_training_plan import _write_workbook

    plan = TrainingPlan(str(_write_workbook(tmp_path / "shift.xlsx", week_header_row=5)))
    db = Database(tmp_path / "shift.db")
    yield plan, db
    db.close()


def _save_run(db: Database, activity_id: int, day: str, km: float = 20.0) -> None:
    db.save_activity(
        activity_id=activity_id, start_time=f"{day} 08:00:00", activity_type="running",
        distance_km=km, duration_seconds=int(km * 400), avg_pace="6:40",
        avg_hr=145, max_hr=160, calories=int(km * 60), aerobic_te=3.0,
        vo2max=None, raw_json="{}", splits_json="[]",
    )


def test_adherence_credits_a_long_run_moved_to_sunday(shift_env):
    """Saturday's slot is completed by the Sunday run, not missed."""
    plan, db = shift_env
    _save_run(db, 1, "2026-03-08")  # Sunday
    adherence = compute_adherence(plan, db, date(2026, 3, 9), lookback_runs=2)
    assert adherence["completed"] == 1
    assert "2026-03-07" not in adherence["missed_dates"]
    # Tuesday genuinely wasn't run, and still shows as missed.
    assert adherence["missed_dates"] == ["2026-03-03"]


def test_adherence_still_flags_a_genuinely_missed_run(shift_env):
    """Nothing run at all → both slots missed. The shift tolerance must not
    quietly forgive skipped weeks."""
    plan, db = shift_env
    adherence = compute_adherence(plan, db, date(2026, 3, 9), lookback_runs=2)
    assert adherence["completed"] == 0
    assert adherence["missed_dates"] == ["2026-03-07", "2026-03-03"]


def test_adherence_does_not_let_one_run_cover_two_slots(shift_env):
    """A Sunday run credits Saturday; Tuesday stays missed even though it's the
    only slot left. One run, one slot."""
    plan, db = shift_env
    _save_run(db, 1, "2026-03-08")
    adherence = compute_adherence(plan, db, date(2026, 3, 9), lookback_runs=2)
    assert adherence["total"] == 2
    assert adherence["completed"] == 1


@pytest.fixture
def shift_env_two_weeks(tmp_path):
    """Two consecutive plan weeks (Tue + Sat runs each), for shifts that cross
    the week boundary — a Saturday long run done the following Monday."""
    from test_training_plan import _write_two_week_workbook

    plan = TrainingPlan(str(_write_two_week_workbook(tmp_path / "two_week.xlsx")))
    db = Database(tmp_path / "two_week.db")
    yield plan, db
    db.close()


def test_adherence_credits_a_long_run_moved_across_the_week_boundary(shift_env_two_weeks):
    """A Saturday long run done two days later, the following Monday, crosses
    into the next plan week. It must still credit week 1's Saturday slot —
    not book that week as a miss while week 2 gets an unprescribed extra."""
    plan, db = shift_env_two_weeks
    _save_run(db, 1, "2026-03-03")  # Tue, week 1 — its own slot
    _save_run(db, 2, "2026-03-09", km=20.0)  # Mon, week 2 — week 1's delayed long run
    _save_run(db, 3, "2026-03-10")  # Tue, week 2 — its own slot

    adherence = compute_adherence(plan, db, date(2026, 3, 16), lookback_runs=4)
    assert adherence["completed"] == 3
    assert "2026-03-07" not in adherence["missed_dates"]  # week 1's Saturday, done on the 9th
    assert adherence["missed_dates"] == ["2026-03-14"]  # week 2's Saturday genuinely missed


def test_analyze_run_prompt_carries_the_prescription_of_a_moved_run(shift_env):
    """The whole point: a Sunday long run is judged against Saturday's slot."""
    plan, db = shift_env
    _save_run(db, 1, "2026-03-08")
    c = Coach(api_key="test-key", plan=plan, db=db)
    c.client = MagicMock()
    _wire_stream(c.client, _response([_block("text", "ok")]))
    c.analyze_run({
        "start_time": "2026-03-08 08:00:00",
        "distance_km": 20.0,
        "duration_seconds": 8000,
        "avg_pace_min_km": "6:40",
        "avg_hr": 145,
        "max_hr": 160,
        "splits_json": "[]",
        "hr_zones_json": None,
    })
    prompt = c.client.messages.stream.call_args.kwargs["messages"][-1]["content"]
    assert "No run prescribed" not in prompt
    assert "Long 20 km" in prompt
    assert "carried over from Saturday Mar 07" in prompt
    # The model has to be told not to read the day mismatch as a missed session.
    assert "not missed" in prompt
