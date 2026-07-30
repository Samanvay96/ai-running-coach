"""Tests for Garmin HR-zone ingestion and the Z2 band the coach quotes.

Background: the coach derived Zone 2 from MAX_HR = 220 - age = 190, producing a
132-146 bpm band. Garmin has this runner's zones configured from max HR 199 with
RHR 44, i.e. Z2 = 137-152. The gap made correctly-easy runs (avg HR 139-143, 87-100%
of time in Z1+Z2) read as "ran too hard", and put the band we printed in the prompt
5 bpm below the Garmin zone buckets whose percentages we printed beside it.

These tests pin: the payload parser (including the failure paths — this is a network
fetch that can return anything), the source precedence in resolve_z2_bounds, and the
provenance string that makes a stale or missing fetch visible.

Run with: `.venv/bin/python -m pytest tests/test_hr_zones.py`
"""

import json
import sqlite3
from pathlib import Path

import pytest

from src.coach import resolve_z2_bounds
from src.config import MAX_HR
from src.db import Database
from src.garmin_client import select_hr_zone_entry
from src.training_plan import TrainingPlan

# Gitignored: holds personal training data, so absent from a fresh clone.
V7 = "Lisbon_Marathon_Finish_Plan_v7.xlsx"


def _load_or_skip(path: str) -> TrainingPlan:
    if not Path(path).exists():
        pytest.skip(f"{path} not present (gitignored — personal training data)")
    return TrainingPlan(path)


# The real payload, as returned by /biometric-service/heartRateZones.
RUNNING_ENTRY = {
    "trainingMethod": "HR_RESERVE",
    "restingHeartRateUsed": 44,
    "lactateThresholdHeartRateUsed": None,
    "zone1Floor": 122, "zone2Floor": 137, "zone3Floor": 153,
    "zone4Floor": 168, "zone5Floor": 184,
    "maxHeartRateUsed": 199,
    "restingHrAutoUpdateUsed": True,
    "sport": "RUNNING",
    "changeState": "UNCHANGED",
}
DEFAULT_ENTRY = {**RUNNING_ENTRY, "sport": "DEFAULT"}


@pytest.fixture
def db(tmp_path) -> Database:
    d = Database(tmp_path / "test.db")
    yield d
    d.close()


@pytest.fixture(scope="module")
def plan() -> TrainingPlan:
    return _load_or_skip(V7)


def _store(db: Database, entry: dict, fetched_date: str = "2026-07-30"):
    db.save_hr_zones(
        fetched_date=fetched_date,
        sport=entry.get("sport"),
        training_method=entry.get("trainingMethod"),
        max_hr=entry.get("maxHeartRateUsed"),
        resting_hr=entry.get("restingHeartRateUsed"),
        floors={n: entry.get(f"zone{n}Floor") for n in range(1, 6)},
        raw_json=json.dumps(entry),
    )


# --- Payload selection ---


def test_running_entry_wins_over_default():
    """A sport-specific override is the one actually applied to runs."""
    modified = {**RUNNING_ENTRY, "zone2Floor": 140}
    got = select_hr_zone_entry([DEFAULT_ENTRY, modified])
    assert got["sport"] == "RUNNING"
    assert got["zone2Floor"] == 140


def test_default_used_when_no_running_entry():
    assert select_hr_zone_entry([DEFAULT_ENTRY])["sport"] == "DEFAULT"


def test_unknown_sport_still_usable_as_last_resort():
    assert select_hr_zone_entry([{**RUNNING_ENTRY, "sport": "CYCLING"}])["sport"] == "CYCLING"


@pytest.mark.parametrize("payload", [
    None,                                   # fetch failed / retry exhausted
    [],                                     # empty list
    {},                                     # dict instead of list
    "not json",                             # garbage
    [None, "x"],                            # list of non-dicts
    [{"sport": "RUNNING"}],                 # no zone floors at all
    [{**RUNNING_ENTRY, "zone2Floor": None}],  # missing the floor we need
    [{**RUNNING_ENTRY, "zone3Floor": 0}],   # falsy ceiling
])
def test_unusable_payloads_return_none(payload):
    """Anything we can't derive a band from must fall through to None so the
    caller falls back rather than persisting a half-parsed row."""
    assert select_hr_zone_entry(payload) is None


# --- Storage round-trip ---


def test_zone_snapshot_round_trip(db):
    _store(db, RUNNING_ENTRY)
    got = db.get_latest_hr_zones()
    assert got["max_hr"] == 199
    assert got["resting_hr"] == 44
    assert got["zone2_floor"] == 137
    assert got["zone3_floor"] == 153
    assert got["training_method"] == "HR_RESERVE"
    assert got["sport"] == "RUNNING"


def test_latest_snapshot_wins(db):
    _store(db, RUNNING_ENTRY, fetched_date="2026-07-01")
    _store(db, {**RUNNING_ENTRY, "maxHeartRateUsed": 201, "zone2Floor": 139}, fetched_date="2026-07-30")
    got = db.get_latest_hr_zones()
    assert got["max_hr"] == 201
    assert got["zone2_floor"] == 139


def test_same_day_refetch_replaces_rather_than_duplicates(db):
    _store(db, RUNNING_ENTRY)
    _store(db, {**RUNNING_ENTRY, "maxHeartRateUsed": 200})
    rows = db.conn.execute("SELECT COUNT(*) FROM hr_zones").fetchone()[0]
    assert rows == 1
    assert db.get_latest_hr_zones()["max_hr"] == 200


def test_no_snapshot_returns_none(db):
    assert db.get_latest_hr_zones() is None


# --- resolve_z2_bounds precedence ---


def test_garmin_zones_give_the_real_band(db, plan):
    """The whole point: 137-152, not the 132-146 that 220-age produced."""
    _store(db, RUNNING_ENTRY)
    low, high, provenance = resolve_z2_bounds(db, plan)
    assert (low, high) == (137, 152)
    assert "Garmin-configured" in provenance
    assert "199" in provenance and "HR_RESERVE" in provenance


def test_band_is_z2_floor_to_just_below_z3_floor(db, plan):
    """The ceiling must not overlap Z3 — 152, not 153."""
    _store(db, {**RUNNING_ENTRY, "zone2Floor": 140, "zone3Floor": 160})
    low, high, _ = resolve_z2_bounds(db, plan)
    assert (low, high) == (140, 159)


def test_garmin_zones_beat_the_plan_derived_band(db, plan):
    """Even with wellness RHR present, the Garmin snapshot takes precedence."""
    db.save_daily_wellness("2026-07-30", None, None, None, None, None, 44, "{}")
    _store(db, RUNNING_ENTRY)
    low, high, provenance = resolve_z2_bounds(db, plan)
    assert (low, high) == (137, 152)
    assert "fallback" not in provenance


def test_falls_back_to_plan_percentages_without_a_snapshot(db, plan):
    """No Garmin data: use the Pace Guide percentages on 220-age."""
    db.save_daily_wellness("2026-07-30", None, None, None, None, None, 44, "{}")
    low, high, provenance = resolve_z2_bounds(db, plan)
    assert (low, high) == plan.get_z2_bounds(MAX_HR, 44)
    assert "fallback" in provenance
    assert str(MAX_HR) in provenance


def test_fallback_provenance_warns_that_the_band_may_be_off(db, plan):
    """A silent fallback would quietly reintroduce the original bug, so the
    provenance has to say so loudly enough for the model to hedge."""
    _, _, provenance = resolve_z2_bounds(db, plan)
    assert "⚠️" in provenance
    assert "unavailable" in provenance


def test_fallback_without_rhr_still_resolves(db, plan):
    """No wellness row at all — %MaxHR path rather than Karvonen."""
    low, high, provenance = resolve_z2_bounds(db, plan)
    assert low < high
    assert "RHR unavailable" in provenance


def test_returns_none_when_no_source_can_produce_a_band(db, plan):
    """Empty pace zones and no Garmin snapshot — callers must handle None
    rather than get a bogus band."""
    stripped = _load_or_skip(V7)
    stripped.pace_zones = []
    assert resolve_z2_bounds(db, stripped) is None


def test_partial_snapshot_falls_back(db, plan):
    """A row missing zone3_floor can't define a ceiling; don't invent one."""
    db.conn.execute(
        "INSERT INTO hr_zones (fetched_date, zone2_floor, zone3_floor) VALUES (?, ?, ?)",
        ("2026-07-30", 137, None),
    )
    db.conn.commit()
    _, _, provenance = resolve_z2_bounds(db, plan)
    assert "fallback" in provenance


# --- The regression this whole change exists to fix ---


@pytest.mark.parametrize("date,avg_hr,pace", [
    ("2026-07-30", 143, "6:21"),
    ("2026-07-28", 141, "6:28"),
    ("2026-07-25", 139, "6:48"),
    ("2026-07-21", 139, "6:32"),
])
def test_recent_easy_runs_sit_inside_the_corrected_band(db, plan, date, avg_hr, pace):
    """These are the runs that were being flagged as too fast. Under the correct
    band every one of them is comfortably inside Zone 2."""
    _store(db, RUNNING_ENTRY)
    low, high, _ = resolve_z2_bounds(db, plan)
    assert low <= avg_hr <= high, f"{date} ({pace}/km, HR {avg_hr}) should be Z2"


def test_old_band_would_have_misjudged_them(plan):
    """Guard the premise: under 220-age these same runs sat at or over the
    ceiling. If this ever stops being true the fix has lost its motivation."""
    old_low, old_high = plan.get_z2_bounds(MAX_HR, 44)
    assert old_high < 152, "220-age band should be tighter than Garmin's"
    assert old_high <= 146
