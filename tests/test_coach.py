"""Smoke tests for the LLM call sites in src/coach.py.

The Apr 28 silent-failure bug (`StopIteration` from `next(b.text for b in
response.content if b.type == "text")` when the model emitted only thinking
blocks) is the regression these tests are designed to catch. If you change
how `_extract_text` handles missing text blocks, run these.

Run with: `.venv/bin/python -m pytest tests/`
"""

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.coach import (
    Coach,
    _extract_text,
    compute_zone_distribution,
    format_feel,
    format_weather,
    heat_note,
    resolve_runner_today,
)
from src.config import DB_PATH, TRAINING_PLAN_PATH
from src.db import Database
from src.training_plan import TrainingPlan


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
    plan = TrainingPlan(str(TRAINING_PLAN_PATH))
    db = Database(DB_PATH)
    c = Coach(api_key="test-key", plan=plan, db=db)
    c.client = MagicMock()
    return c


def test_analyze_run_happy_path_returns_text(coach):
    """End-to-end: analyze_run builds a prompt, calls API, extracts text."""
    coach.client.messages.create.return_value = _response([
        _block("thinking", "..."),
        _block("text", "Verdict: solid run."),
    ])
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
    assert coach.client.messages.create.called

    # Sanity-check the request shape that the production bug depended on
    call_kwargs = coach.client.messages.create.call_args.kwargs
    assert call_kwargs["max_tokens"] >= 4096, (
        "max_tokens must stay generous to leave headroom for adaptive thinking + text"
    )
    assert call_kwargs["thinking"] == {"type": "adaptive"}, (
        "budget_tokens is rejected by the API for adaptive — see Apr 28 regression"
    )


def test_analyze_run_propagates_no_text_failure(coach):
    """If the model returns only thinking, the failure must reach the caller
    (poller's outer try/except) instead of being silently swallowed."""
    coach.client.messages.create.return_value = _response(
        [_block("thinking", "...")], stop_reason="max_tokens"
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
    coach.client.messages.create.return_value = _response([_block("text", "ok")])
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
    prompt = coach.client.messages.create.call_args.kwargs["messages"][-1]["content"]
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
