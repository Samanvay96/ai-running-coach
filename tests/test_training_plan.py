"""Tests for the YAML plan loader.

The plan moved from xlsx to a hand-edited plan.yaml. The loader's job is now
validation rather than cell-text guessing: a bad edit must fail loudly, naming
the field, and must not leave a half-loaded plan behind on reload.

Run with: `.venv/bin/python -m pytest tests/test_training_plan.py`
"""

import copy
import os
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from src.training_plan import GuidanceBlock, PlanError, TrainingPlan

# plan.yaml holds personal training data and is gitignored. Tests about what the
# real plan says skip without it; everything else builds a synthetic plan.
REAL_PLAN = Path(__file__).resolve().parents[1] / "plan.yaml"

LOADING = "Foot/calf loading (PF protocol ~20 min)"


def _base_plan() -> dict:
    """One week, Mon 2026-03-02 – Sun 03-08, running Tue (easy 5) and Sat
    (long 20 with an MP finish). The race sits outside the window."""
    return {
        "title": "TEST PLAN (v9)",
        "revision": "v9 (Aug 01): test fixture.",
        "race": {"name": "Test Marathon", "date": date(2026, 10, 10),
                 "goal": "4:30", "goal_pace": "6:24/km"},
        "zones": [
            {"type": "easy", "label": "Easy / Recovery", "pace": "7:00–7:30/km",
             "hr": "Zone 2 (60-70% max HR)", "feel": "Conversational."},
            {"type": "long", "label": "Long Run", "pace": "7:00–7:20/km",
             "hr": "Zone 2 (60-70% max HR)", "feel": "Conversational."},
            {"type": "mp", "label": "Marathon Pace (MP)", "pace": "6:45/km",
             "hr": "Zone 2-3", "feel": "Steady."},
        ],
        "benchmarks": [{"checkpoint": "Long run 20 km", "target": "Complete",
                        "why": "Because.", "when": "Wk 1"}],
        "guidance": {"TEST RULES": ["• Rule one.", "• Rule two."]},
        "race_day": {
            "splits": [{"segment": "0–5 km", "pace": "6:30/km (steady)", "cumulative": "0:32:30"}],
            "fuelling": [{"when": "Km 8", "what": "Gel #1", "notes": "With water."}],
        },
        "weeks": [{
            "n": 1, "start": date(2026, 3, 2), "phase": "Test",
            "banner": "PHASE 1: TESTING (Weeks 1–1)",
            "target_km": 25, "notes": "Fixture week.",
            "mon": LOADING,
            "tue": {"type": "easy", "km": 5},
            "sat": {"type": "long", "km": 20, "finish": {"km": 4, "pace": "6:45/km"}},
        }],
    }


def _dump(path: Path, plan: dict) -> Path:
    path.write_text(yaml.safe_dump(plan, allow_unicode=True, sort_keys=False))
    return path


def _write_plan(path, plan: dict | None = None) -> Path:
    return _dump(Path(path), plan or _base_plan())


def _write_two_week_plan(path) -> Path:
    """Two consecutive weeks (Tue easy 5 + Sat long 20 / 22), for shifts that
    cross the week boundary. Mon 2026-03-02..Sun 03-08 then Mon 03-09..Sun 03-15."""
    plan = _base_plan()
    plan["weeks"] = [
        {"n": 1, "start": date(2026, 3, 2), "phase": "Test", "target_km": 25,
         "tue": {"type": "easy", "km": 5}, "sat": {"type": "long", "km": 20}},
        {"n": 2, "start": date(2026, 3, 9), "phase": "Test", "target_km": 27,
         "tue": {"type": "easy", "km": 5}, "sat": {"type": "long", "km": 22}},
    ]
    return _dump(Path(path), plan)


def _load(tmp_path, mutate) -> TrainingPlan:
    plan = _base_plan()
    mutate(plan)
    return TrainingPlan(str(_dump(tmp_path / "p.yaml", plan)))


@pytest.fixture
def plan(tmp_path) -> TrainingPlan:
    return TrainingPlan(str(_write_plan(tmp_path / "p.yaml")))


# --- What a loaded plan exposes ---


def test_week_fields(plan):
    week = plan.weeks[0]
    assert week.week_number == 1
    assert week.start_date == date(2026, 3, 2)
    assert week.end_date == date(2026, 3, 8)
    assert week.dates == "Mar 02 – Mar 08"
    assert week.weekly_km_target == 25
    assert week.notes == "Fixture week."
    assert plan.get_section_marker(1) == "PHASE 1: TESTING (Weeks 1–1)"


def test_missing_days_are_rest_and_strings_are_non_run_days(plan):
    week = plan.weeks[0]
    assert week.sunday.workout_type == "rest"
    assert week.sunday.description == "Rest"
    assert week.monday.workout_type == "rest"
    assert week.monday.description == LOADING
    assert [i for i, _ in week.run_slots()] == [1, 5]


def test_session_takes_its_zone_pace_by_default(plan):
    tue = plan.weeks[0].tuesday
    assert tue.workout_type == "easy"
    assert tue.distance_km == 5
    assert tue.target_pace == "7:00–7:30/km"
    assert tue.description == "Easy 5 km @ 7:00–7:30/km"


def test_long_run_finish_segment(plan):
    sat = plan.weeks[0].saturday
    assert sat.target_pace == "7:00–7:20/km"
    assert (sat.finish_km, sat.finish_pace) == (4, "6:45/km")
    assert sat.pace_brief() == "7:00–7:20/km, last 4 km @ 6:45/km"
    assert sat.description == "Long 20 km @ 7:00–7:20/km — last 4 km @ 6:45/km"


def test_explicit_pace_and_note_override_the_zone(tmp_path):
    p = _load(tmp_path, lambda d: d["weeks"][0].update(
        tue={"type": "easy", "km": 5, "pace": "6:30/km", "note": "HR ≤150"}))
    assert p.weeks[0].tuesday.target_pace == "6:30/km"
    assert p.weeks[0].tuesday.description == "Easy 5 km @ 6:30/km — HR ≤150"


def test_easy_pace_is_picked_by_distance_band(tmp_path):
    def mutate(d):
        d["zones"][0]["km"] = [0, 6.9]
        d["zones"].insert(1, {"type": "easy", "label": "Easy 7+", "km": [7, 11.9],
                              "pace": "6:40–7:05/km", "hr": "Zone 2", "feel": ""})
        d["weeks"][0].update(tue={"type": "easy", "km": 5}, thu={"type": "easy", "km": 8})
    p = _load(tmp_path, mutate)
    assert p.weeks[0].tuesday.target_pace == "7:00–7:30/km"
    assert p.weeks[0].thursday.target_pace == "6:40–7:05/km"


def test_goal_race_gets_the_opening_split_but_a_tune_up_does_not(tmp_path):
    def mutate(d):
        d["race"]["date"] = date(2026, 3, 8)
        d["weeks"][0]["sun"] = {"type": "race", "km": 42.2, "name": "Test Marathon"}
        d["weeks"][0]["thu"] = {"type": "race", "km": 21.1, "name": "Tune-up half"}
    p = _load(tmp_path, mutate)
    assert p.weeks[0].sunday.target_pace == "6:30/km"
    assert p.weeks[0].sunday.description == "🏁 Test Marathon 42.2 km @ 6:30/km"
    assert p.weeks[0].thursday.target_pace == ""
    assert p.weeks[0].thursday.description == "🏁 Tune-up half 21.1 km"


def test_goal_race_falls_back_to_mp_without_splits(tmp_path):
    def mutate(d):
        d["race"]["date"] = date(2026, 3, 8)
        d["race_day"]["splits"] = []
        d["weeks"][0]["sun"] = {"type": "race", "km": 42.2, "name": "Test Marathon"}
    assert _load(tmp_path, mutate).weeks[0].sunday.target_pace == "6:45/km"


def test_plan_metadata(plan):
    assert plan.title == "TEST PLAN (v9)"
    assert plan.revision_note == "v9 (Aug 01): test fixture."
    assert plan.race_name == "Test Marathon"
    assert plan.race_date == date(2026, 10, 10)
    assert plan.start_date == date(2026, 3, 2)
    assert plan.target_finish == "4:30"
    assert plan.target_pace == "6:24/km"
    assert plan.get_goal_summary() == \
        "Test Marathon on Saturday October 10, 2026 — target 4:30 (~6:24/km)"


def test_goal_summary_without_a_pace(tmp_path):
    p = _load(tmp_path, lambda d: d["race"].update(goal="TBD after checkpoint", goal_pace=None))
    assert p.get_goal_summary() == "Test Marathon on Saturday October 10, 2026 — goal: TBD after checkpoint"


def test_race_tables_benchmarks_and_guidance(plan):
    assert [s.segment for s in plan.race_splits] == ["0–5 km"]
    assert [f.when for f in plan.fueling] == ["Km 8"]
    assert plan.get_benchmarks_text() == "  Long run 20 km: Complete — Because. [Wk 1]"
    assert plan.guidance == [GuidanceBlock("TEST RULES", ["• Rule one.", "• Rule two."])]
    assert plan.get_guidance_text() == "TEST RULES\n  • Rule one.\n  • Rule two."


def test_optional_sections_may_be_absent(tmp_path):
    def mutate(d):
        for key in ("benchmarks", "guidance", "race_day", "revision"):
            del d[key]
    p = _load(tmp_path, mutate)
    assert p.benchmarks == [] and p.guidance == [] and p.race_splits == [] and p.fueling == []
    assert p.get_benchmarks_text() == ""


def test_weeks_crossing_new_year_keep_their_real_dates(tmp_path):
    """The xlsx parser assumed one calendar year and put Jan–Apr in the wrong
    year. ISO dates make that impossible; guard it anyway."""
    def mutate(d):
        d["weeks"] = [
            {"n": 1, "start": date(2026, 12, 28), "tue": {"type": "easy", "km": 5}},
            {"n": 2, "start": date(2027, 1, 4), "tue": {"type": "easy", "km": 6}},
        ]
    p = _load(tmp_path, mutate)
    assert p.weeks[0].dates == "Dec 28 – Jan 03"
    assert p.weeks[1].start_date == date(2027, 1, 4)
    assert p.get_prescribed_run(date(2027, 1, 5)).distance_km == 6


def test_z2_bounds_from_the_easy_zone(plan):
    assert plan.get_z2_bounds(199, 44) == (137, 152)
    assert plan.get_z2_bounds(200) == (120, 140)


def test_week_summary(plan):
    summary = plan.get_week_summary(plan.weeks[0])
    assert "PHASE 1: TESTING" in summary
    assert f"Mon: {LOADING}" in summary
    assert "Target: 25.0 km" in summary


def test_guidance_block_defaults_to_no_lines():
    assert GuidanceBlock(title="X").lines == []


# --- Validation: a bad edit fails loudly and names the field ---


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d["weeks"][0].update(tue={"type": "eazy", "km": 5}), "weeks[0].tue.type: 'eazy'"),
    (lambda d: d["weeks"][0].update(tue={"type": "easy"}), "weeks[0].tue: missing 'km'"),
    (lambda d: d["weeks"][0].update(tue={"type": "easy", "km": 0}), "weeks[0].tue.km: must be positive"),
    (lambda d: d["weeks"][0].update(tue={"type": "easy", "km": "5"}), "weeks[0].tue.km: expected int or float"),
    (lambda d: d["weeks"][0].update(tue={"type": "easy", "km": 5, "dist": 3}), "unknown keys ['dist']"),
    (lambda d: d["weeks"][0].update(start=date(2026, 3, 3)), "is a Tuesday, not a Monday"),
    (lambda d: d["weeks"][0].update(tuesday={"type": "easy", "km": 5}), "unknown keys ['tuesday']"),
    (lambda d: d["weeks"][0].update(sat={"type": "long", "km": 20, "finish": {"km": 4}}),
     "weeks[0].sat.finish: missing 'pace'"),
    (lambda d: d["weeks"].clear(), "the plan has no weeks"),
    (lambda d: d.pop("race"), "plan: missing 'race'"),
    (lambda d: d["race"].update(date="2026-10-10x"), "race.date: expected date"),
    (lambda d: d["guidance"].update({"BAD": ["• ok", {"First 25%": "slow"}]}), "guidance.BAD"),
])
def test_invalid_plans_are_rejected_with_the_field_named(tmp_path, mutate, message):
    with pytest.raises(PlanError) as exc:
        _load(tmp_path, mutate)
    assert message in str(exc.value)


def test_an_unquoted_pace_is_caught(tmp_path):
    """PyYAML reads a bare 6:45 as the base-60 integer 405."""
    path = _write_plan(tmp_path / "p.yaml")
    path.write_text(path.read_text().replace("pace: 6:24/km", "pace: 6:24"))
    with pytest.raises(PlanError, match="quote paces"):
        TrainingPlan(str(path))


def test_weeks_must_be_contiguous(tmp_path):
    def gap(d):
        d["weeks"].append({"n": 2, "start": date(2026, 3, 16)})
    with pytest.raises(PlanError, match="gap or overlap"):
        _load(tmp_path, gap)

    def misnumbered(d):
        d["weeks"].append({"n": 3, "start": date(2026, 3, 9)})
    with pytest.raises(PlanError, match="numbering must be consecutive"):
        _load(tmp_path, misnumbered)


def test_race_date_inside_the_plan_must_be_a_race_session(tmp_path):
    with pytest.raises(PlanError, match="not a race session in week 1"):
        _load(tmp_path, lambda d: d["race"].update(date=date(2026, 3, 7)))


def test_not_yaml_at_all(tmp_path):
    path = tmp_path / "p.yaml"
    path.write_text("weeks: [unclosed")
    with pytest.raises(PlanError, match="not valid YAML"):
        TrainingPlan(str(path))


# --- Reload ---


def _bump_mtime(path: Path):
    # mtime has 1s granularity on some filesystems; force a distinct value.
    stat = os.stat(path)
    os.utime(path, (stat.st_atime + 10, stat.st_mtime + 10))


def test_reload_picks_up_an_edit(tmp_path):
    path = _write_plan(tmp_path / "p.yaml")
    plan = TrainingPlan(str(path))
    assert plan.weeks[0].weekly_km_target == 25

    edited = _base_plan()
    edited["weeks"][0]["target_km"] = 30
    _dump(path, edited)
    _bump_mtime(path)

    assert plan.reload_if_changed() is True
    assert plan.weeks[0].weekly_km_target == 30
    assert plan.reload_if_changed() is False


def test_a_broken_edit_keeps_the_last_good_plan(tmp_path):
    """The bot reloads on every command. A typo mid-edit must not blank the
    plan or leave it half-replaced."""
    path = _write_plan(tmp_path / "p.yaml")
    plan = TrainingPlan(str(path))

    broken = _base_plan()
    broken["zones"][0]["pace"] = "5:00/km"
    broken["weeks"][0]["tue"] = {"type": "eazy", "km": 5}
    _dump(path, broken)
    _bump_mtime(path)

    with pytest.raises(PlanError):
        plan.reload_if_changed()
    assert plan.weeks[0].tuesday.target_pace == "7:00–7:30/km"
    assert plan.pace_zones[0].pace == "7:00–7:30/km"
    assert plan.race_date == date(2026, 10, 10)


def test_reload_on_a_vanished_file_returns_false(tmp_path):
    path = _write_plan(tmp_path / "gone.yaml")
    plan = TrainingPlan(str(path))
    path.unlink()
    assert plan.reload_if_changed() is False
    assert len(plan.weeks) == 1


def test_reload_does_not_duplicate_anything(tmp_path):
    path = _write_plan(tmp_path / "p.yaml")
    plan = TrainingPlan(str(path))
    counts = lambda: (len(plan.weeks), len(plan.pace_zones), len(plan.race_splits),  # noqa: E731
                      len(plan.fueling), len(plan.guidance), len(plan.section_markers),
                      len(plan.benchmarks))
    before = counts()
    plan._load(str(path))
    assert counts() == before


# --- Shift-tolerant slot resolution ---
#
# The plan pins each session to a weekday, but runs move: a Saturday long run
# gets done on Sunday. resolve_run_for_date matches a run to the nearest free
# slot. The fixture week is Mon 2026-03-02 – Sun 03-08, running Tue + Sat.


@pytest.fixture
def shift_plan(tmp_path) -> TrainingPlan:
    return TrainingPlan(str(_write_plan(tmp_path / "shift.yaml")))


def test_resolve_returns_the_days_own_slot_unshifted(shift_plan):
    r = shift_plan.resolve_run_for_date(date(2026, 3, 7))  # Saturday
    assert r is not None
    assert r.run.workout_type == "long"
    assert r.prescribed_date == date(2026, 3, 7)
    assert r.shifted is False
    assert r.shift_note() == ""


def test_sunday_run_resolves_to_saturdays_long_run(shift_plan):
    r = shift_plan.resolve_run_for_date(date(2026, 3, 8))
    assert r is not None
    assert r.run.workout_type == "long"
    assert r.run.distance_km == 20.0
    assert r.prescribed_date == date(2026, 3, 7)
    assert r.shifted is True
    assert r.shift_note() == "carried over from Saturday Mar 07"


def test_sunday_run_does_not_claim_a_slot_saturday_already_filled(shift_plan):
    r = shift_plan.resolve_run_for_date(date(2026, 3, 8), completed_dates={date(2026, 3, 7)})
    assert r is None


def test_friday_run_pulls_saturdays_long_run_forward(shift_plan):
    r = shift_plan.resolve_run_for_date(date(2026, 3, 6))
    assert r is not None
    assert r.run.workout_type == "long"
    assert r.prescribed_date == date(2026, 3, 7)
    assert r.shift_note() == "pulled forward from Saturday Mar 07"


def test_tie_between_flanking_slots_goes_to_the_earlier_one(shift_plan):
    r = shift_plan.resolve_run_for_date(date(2026, 3, 4))
    assert r is not None
    assert r.prescribed_date == date(2026, 3, 3)
    assert r.run.workout_type == "easy"


def test_resolution_will_not_reach_further_than_max_shift_days(shift_plan):
    assert shift_plan.MAX_SHIFT_DAYS == 2
    r = shift_plan.resolve_run_for_date(date(2026, 3, 2), completed_dates={date(2026, 3, 3)})
    assert r is None


def test_resolution_stays_inside_the_plan_window(shift_plan):
    assert shift_plan.resolve_run_for_date(date(2026, 3, 1)) is None
    assert shift_plan.resolve_run_for_date(date(2026, 3, 9)) is None


def test_monday_run_resolves_to_prior_weeks_saturday_long_run(tmp_path):
    """A shift can cross the week boundary: Saturday's long run done the
    following Monday is still a 2-day carry-over. Week 2's Tuesday is marked
    fulfilled, otherwise the nearer slot wins."""
    plan = TrainingPlan(str(_write_two_week_plan(tmp_path / "two_week.yaml")))
    r = plan.resolve_run_for_date(date(2026, 3, 9), completed_dates={date(2026, 3, 10)})
    assert r is not None
    assert r.run.distance_km == 20.0
    assert r.prescribed_date == date(2026, 3, 7)
    assert r.shift_note() == "carried over from Saturday Mar 07"


def test_get_prescribed_run_stays_strict(shift_plan):
    assert shift_plan.get_prescribed_run(date(2026, 3, 8)) is None
    assert shift_plan.get_prescribed_run(date(2026, 3, 7)).workout_type == "long"


def test_non_run_days_never_absorb_a_shifted_run(tmp_path):
    """Travel days are strings, so a run logged mid-trip matches nothing
    rather than being booked against a trekking day."""
    def mutate(d):
        d["weeks"][0] = {"n": 1, "start": date(2026, 3, 2),
                         **{k: "Travel — hiking" for k in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}}
    p = _load(tmp_path, mutate)
    assert p.resolve_run_for_date(date(2026, 3, 4)) is None
    assert p.weeks[0].run_slots() == []


# --- The real plan (skips in a fresh clone) ---


@pytest.fixture(scope="module")
def real() -> TrainingPlan:
    if not REAL_PLAN.exists():
        pytest.skip("plan.yaml not present (gitignored — personal training data)")
    return TrainingPlan(str(REAL_PLAN))


def test_real_plan_targets_manchester(real):
    assert real.race_name == "Manchester Marathon"
    assert real.race_date == date(2027, 4, 18)
    assert real.weeks[-1].sunday.workout_type == "race"


def test_real_plan_weekly_targets_match_the_sessions(real):
    """target_km is hand-written; keep it honest against the sessions."""
    for w in real.weeks:
        total = sum(r.distance_km for _, r in w.run_slots() if r.workout_type != "race")
        assert total == pytest.approx(w.weekly_km_target), f"week {w.week_number}"


def test_real_plan_prescribes_no_runs_while_trekking(real):
    trek = date(2026, 10, 21)
    while trek <= date(2026, 11, 1):
        assert real.get_prescribed_run(trek) is None, trek
        trek += timedelta(days=1)


def test_real_plan_checkpoint_is_a_21km_long_run_on_dec_12(real):
    run = real.get_prescribed_run(date(2026, 12, 12))
    assert run.workout_type == "long" and run.distance_km == 21
    assert "CHECKPOINT" in run.description


def test_real_plan_goal_waits_for_the_checkpoint(real):
    """Setting a goal from short-run evidence is exactly what not to do."""
    assert real.goal_pace == ""
    assert "TBD" in real.goal
    assert not any(r.finish_pace for w in real.weeks for _, r in w.run_slots())


def test_real_plan_long_run_never_jumps_more_than_3km(real):
    longest = [max((r.distance_km for _, r in w.run_slots() if r.workout_type == "long"), default=0)
               for w in real.weeks]
    prior = 18.0  # Sep 06, the longest run before the plan
    for n, km in enumerate(longest, start=1):
        if km:
            assert km - prior <= 3, f"week {n}: {prior} -> {km}"
            prior = max(prior, km)
