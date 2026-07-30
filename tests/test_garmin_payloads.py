"""Tests for the nested Garmin payload parsers.

Two bugs motivated these. Both were silent — they produced None rather than
raising, so the coach simply reported "Not available" forever:

  1. training_status was read off the top level (`weeklyTrainingLoad`,
     `vo2MaxValue`, `trainingStatusLabel`, `recoveryTimeInHours`). None of those
     keys exist there; the real values are nested under mostRecentVO2Max.generic
     and mostRecentTrainingStatus.latestTrainingStatusData.<deviceId>. Every
     snapshot in the DB was a row of NULLs.
  2. lactate threshold was read off the top level too, where the payload is
     actually {"speed_and_heart_rate": {...}, "power": {}}. Separately, `speed`
     is seconds-per-metre despite its name, so the old `1000 / speed` conversion
     would have produced ~53 min/km had it ever run.

The fixtures below are the real payload shapes as returned on 2026-07-30.
Device-ID keys are deliberately arbitrary — they are not knowable in advance and
change when the runner switches watches.

Run with: `.venv/bin/python -m pytest tests/test_garmin_payloads.py`
"""

from datetime import date

import pytest

from src.coach import format_lactate_threshold, format_training_status
from src.garmin_client import parse_lactate_threshold, parse_training_status

DEVICE = "3626776704"

TRAINING_STATUS = {
    "userId": 76635645,
    "mostRecentVO2Max": {
        "userId": 76635645,
        "generic": {
            "calendarDate": "2026-07-30",
            "vo2MaxPreciseValue": 52.2,
            "vo2MaxValue": 52.0,
            "fitnessAge": None,
        },
        "cycling": None,
    },
    "mostRecentTrainingStatus": {
        "userId": 76635645,
        "latestTrainingStatusData": {
            DEVICE: {
                "calendarDate": "2026-07-30",
                "weeklyTrainingLoad": None,
                "trainingStatus": 4,
                "trainingStatusFeedbackPhrase": "MAINTAINING_4",
                "sport": "RUNNING",
                "acuteTrainingLoadDTO": {
                    "acwrPercent": 33,
                    "acwrStatus": "OPTIMAL",
                    "dailyTrainingLoadAcute": 231,
                    "dailyTrainingLoadChronic": 275,
                    "dailyAcuteChronicWorkloadRatio": 0.8,
                },
                "primaryTrainingDevice": True,
            }
        },
    },
    "heatAltitudeAcclimationDTO": None,
}

LACTATE_THRESHOLD = {
    "speed_and_heart_rate": {
        "userProfilePK": 76635645,
        "calendarDate": "2026-03-12T01:05:41.897",
        "speed": 0.3138000011444092,
        "heartRate": 171,
        "heartRateCycling": None,
    },
    "power": {},
}


# --- training status ---


def test_extracts_every_available_field():
    got = parse_training_status(TRAINING_STATUS)
    assert got["vo2max"] == 52.2
    assert got["training_load_7d"] == 231
    assert got["training_status_label"] == "MAINTAINING_4"
    assert got["snapshot_date"] == "2026-07-30"


def test_no_field_comes_back_all_null():
    """The actual bug: every value was None, so the DB filled with empty rows."""
    got = parse_training_status(TRAINING_STATUS)
    populated = [k for k, v in got.items() if v is not None]
    assert len(populated) >= 4, f"only {populated} populated"


def test_prefers_precise_vo2max_but_falls_back():
    coarse = {**TRAINING_STATUS, "mostRecentVO2Max": {"generic": {"vo2MaxValue": 51.0}}}
    assert parse_training_status(coarse)["vo2max"] == 51.0


def test_weekly_load_wins_when_garmin_populates_it():
    """This account reports null, but the field is the more direct answer when present."""
    payload = {**TRAINING_STATUS}
    payload["mostRecentTrainingStatus"] = {
        "latestTrainingStatusData": {
            DEVICE: {**TRAINING_STATUS["mostRecentTrainingStatus"]["latestTrainingStatusData"][DEVICE],
                     "weeklyTrainingLoad": 400}
        }
    }
    assert parse_training_status(payload)["training_load_7d"] == 400


def test_device_id_key_is_not_hardcoded():
    """Keys are device IDs — a new watch changes them."""
    payload = {**TRAINING_STATUS}
    inner = TRAINING_STATUS["mostRecentTrainingStatus"]["latestTrainingStatusData"][DEVICE]
    payload["mostRecentTrainingStatus"] = {"latestTrainingStatusData": {"9999999999": inner}}
    assert parse_training_status(payload)["training_status_label"] == "MAINTAINING_4"


def test_primary_device_wins_when_several_are_recorded():
    inner = TRAINING_STATUS["mostRecentTrainingStatus"]["latestTrainingStatusData"][DEVICE]
    payload = {**TRAINING_STATUS}
    payload["mostRecentTrainingStatus"] = {
        "latestTrainingStatusData": {
            "111": {**inner, "primaryTrainingDevice": False, "trainingStatusFeedbackPhrase": "OLD_WATCH"},
            "222": {**inner, "primaryTrainingDevice": True, "trainingStatusFeedbackPhrase": "CURRENT"},
        }
    }
    assert parse_training_status(payload)["training_status_label"] == "CURRENT"


def test_falls_back_to_any_device_when_none_flagged_primary():
    inner = {"trainingStatusFeedbackPhrase": "ONLY", "calendarDate": "2026-07-30"}
    payload = {"mostRecentTrainingStatus": {"latestTrainingStatusData": {"111": inner}}}
    assert parse_training_status(payload)["training_status_label"] == "ONLY"


def test_recovery_time_is_absent_not_invented():
    """No Garmin payload we fetch carries it — checked training status,
    get_user_summary and the activity detail."""
    assert parse_training_status(TRAINING_STATUS)["recovery_time_hours"] is None


@pytest.mark.parametrize("payload", [None, {}, [], "junk", {"mostRecentVO2Max": None}])
def test_malformed_training_status_does_not_raise(payload):
    got = parse_training_status(payload)
    assert isinstance(got, dict)
    assert got.get("vo2max") is None


def test_missing_device_map_degrades_gracefully():
    payload = {"mostRecentTrainingStatus": {"latestTrainingStatusData": None},
               "mostRecentVO2Max": {"generic": {"vo2MaxValue": 52.0}}}
    got = parse_training_status(payload)
    assert got["vo2max"] == 52.0
    assert got["training_status_label"] is None


# --- lactate threshold ---


def test_lt_pace_uses_seconds_per_metre_not_metres_per_second():
    """0.3138 -> 5:14/km. Read as m/s it would be ~53 min/km, which is what the
    old `1000 / speed` conversion would have produced."""
    got = parse_lactate_threshold(LACTATE_THRESHOLD)
    assert got["lt_pace_min_km"] == pytest.approx(5.23, abs=0.01)
    secs_per_km = got["lt_pace_min_km"] * 60
    assert 300 < secs_per_km < 330


def test_lt_hr_and_date_land():
    got = parse_lactate_threshold(LACTATE_THRESHOLD)
    assert got["lt_hr"] == 171
    assert got["lt_date"] == "2026-03-12"  # timestamp truncated to a date


@pytest.mark.parametrize("speed,expected_none", [
    (0.3138, False),   # 5:14/km — plausible
    (0.12, True),      # 2:00/km — faster than any human threshold pace
    (1.0, True),       # 16:40/km — too slow
    (0, True),         # no data
    (-0.3, True),      # nonsense
    (None, True),
    ("fast", True),
])
def test_implausible_paces_are_rejected(speed, expected_none):
    """A unit misread should drop the value, not feed the coach a bogus threshold."""
    payload = {"speed_and_heart_rate": {"speed": speed, "heartRate": 171}}
    got = parse_lactate_threshold(payload)
    assert (got["lt_pace_min_km"] is None) is expected_none


def test_hr_survives_even_when_pace_is_rejected():
    payload = {"speed_and_heart_rate": {"speed": 999, "heartRate": 171}}
    got = parse_lactate_threshold(payload)
    assert got["lt_pace_min_km"] is None
    assert got["lt_hr"] == 171


@pytest.mark.parametrize("payload", [
    None, {}, [], "junk",
    {"power": {}},                        # the nested key we need is absent
    {"speed_and_heart_rate": None},
    {"speed_and_heart_rate": "junk"},
    {"lactateThresholdSpeed": 0.3138},    # the OLD (wrong) flat shape
])
def test_malformed_lt_does_not_raise(payload):
    assert parse_lactate_threshold(payload).get("lt_pace_min_km") is None


def test_flat_payload_yields_nothing():
    """Guards the premise: the shape the old code expected carries no data, which
    is exactly why LT never landed."""
    got = parse_lactate_threshold({"lactateThresholdSpeed": 0.3138, "heartRate": 171})
    assert got == {} or got.get("lt_hr") is None


# --- prompt formatting ---


def test_training_status_line_omits_missing_fields():
    """Recovery time is genuinely unavailable — it must not render as 'Noneh'."""
    text = format_training_status(parse_training_status(TRAINING_STATUS))
    assert "None" not in text
    assert "Recovery" not in text
    assert "VO2max: 52.2" in text
    assert "Status: MAINTAINING_4" in text
    assert "7-day load: 231" in text


def test_training_status_line_includes_recovery_when_present():
    assert "Recovery: 18h" in format_training_status({"recovery_time_hours": 18})


@pytest.mark.parametrize("ts", [None, {}, {"vo2max": None, "training_status_label": None}])
def test_training_status_line_handles_empty(ts):
    assert format_training_status(ts) == "Not available"


def test_lt_line_flags_a_stale_estimate():
    """The March estimate predates the layoff and the whole current block."""
    ts = parse_lactate_threshold(LACTATE_THRESHOLD)
    line = format_lactate_threshold(ts, date(2026, 7, 30))
    assert "5:14/km" in line
    assert "171 bpm" in line
    assert "measured 2026-03-12" in line
    assert "⚠️" in line and "140 days old" in line


def test_lt_line_does_not_warn_on_a_fresh_estimate():
    ts = {"lt_pace_min_km": 5.23, "lt_hr": 171, "lt_date": "2026-07-20"}
    line = format_lactate_threshold(ts, date(2026, 7, 30))
    assert "⚠️" not in line
    assert "5:14/km" in line


def test_lt_line_empty_without_data():
    assert format_lactate_threshold(None, date(2026, 7, 30)) == ""
    assert format_lactate_threshold({"lt_pace_min_km": None, "lt_hr": None}, date(2026, 7, 30)) == ""


def test_latest_snapshot_wins_within_the_same_day(tmp_path):
    """The poller writes a row per poll, so a day holds ~24 rows sharing a
    snapshot_date. Ordering by date alone returned an arbitrary one — in practice
    the oldest — which silently defeated the parser fix: the freshly-parsed row
    was written but the coach kept reading a legacy all-NULL row from the same day.
    """
    from src.db import Database
    db = Database(tmp_path / "ts.db")
    try:
        for _ in range(5):  # the legacy all-NULL rows
            db.save_training_status("2026-07-30", None, None, None, None, "{}")
        db.save_training_status("2026-07-30", 231, None, 52.2, "MAINTAINING_4", "{}",
                                lt_pace_min_km=5.23, lt_hr=171, lt_date="2026-03-12")
        got = db.get_latest_training_status()
        assert got["vo2max"] == 52.2
        assert got["training_load_7d"] == 231
        assert got["lt_hr"] == 171
    finally:
        db.close()


def test_newer_date_still_wins_over_a_higher_id(tmp_path):
    """The id tie-break must not override the date ordering."""
    from src.db import Database
    db = Database(tmp_path / "ts2.db")
    try:
        db.save_training_status("2026-07-30", 231, None, 52.2, "CURRENT", "{}")
        db.save_training_status("2026-07-01", 100, None, 48.0, "OLD", "{}")  # higher id, older date
        assert db.get_latest_training_status()["training_status_label"] == "CURRENT"
    finally:
        db.close()


def test_lt_line_survives_a_malformed_date():
    line = format_lactate_threshold(
        {"lt_pace_min_km": 5.23, "lt_hr": 171, "lt_date": "not-a-date"}, date(2026, 7, 30)
    )
    assert "5:14/km" in line
    assert "⚠️" not in line
