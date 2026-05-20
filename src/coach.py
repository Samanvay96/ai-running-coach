import json
import logging
from datetime import date, datetime, timedelta, timezone

import anthropic

from .config import RACE_DATE, PLAN_START_DATE, MAX_HR, RUNNER_TIMEZONE, RUNNER_TZ
from .db import Database
from .time_utils import format_utc_offset
from .training_plan import TrainingPlan

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"


def _extract_text(response) -> str:
    """Return the first text block's content. Raises with diagnostics if none.

    With adaptive thinking, the model may emit only thinking blocks if
    max_tokens is exhausted before it gets to the text response — in which
    case `next(...)` would raise StopIteration with an empty str(). This
    helper surfaces stop_reason and block types so failures are debuggable.
    """
    for block in response.content:
        if getattr(block, "type", None) == "text":
            return block.text
    block_types = [getattr(b, "type", "?") for b in response.content]
    stop_reason = getattr(response, "stop_reason", "?")
    usage = getattr(response, "usage", "?")
    raise RuntimeError(
        f"No text block in Anthropic response (stop_reason={stop_reason}, "
        f"blocks={block_types}, usage={usage}). "
        f"Likely cause: max_tokens hit during thinking — increase max_tokens."
    )


def resolve_runner_today(db: Database, within_days: int = 14) -> tuple[date, str]:
    """Return (today, source_label) for the runner's current timezone.

    Priority:
      1. Most recent activity's UTC offset (within `within_days`) — auto-tracks
         travel without the user having to update env.
      2. RUNNER_TIMEZONE env var — fallback for new installs or long gaps
         between runs.
      3. UTC — last resort if neither signal is available.

    source_label is for the system prompt so the model sees what 'today'
    is based on and we can spot when a stale offset is leaking in.
    """
    offset = db.get_latest_tz_offset_minutes(within_days=within_days)
    if offset is not None:
        tz = timezone(timedelta(minutes=offset))
        today = datetime.now(tz).date()
        return today, f"UTC{format_utc_offset(offset)} (auto-derived from latest run)"
    today = datetime.now(RUNNER_TZ).date()
    if RUNNER_TIMEZONE == "UTC":
        return today, "UTC (no recent run, RUNNER_TIMEZONE unset)"
    return today, f"{RUNNER_TIMEZONE} (from RUNNER_TIMEZONE env var)"


def format_pace(speed_mps: float) -> str:
    """Convert m/s to min:sec/km."""
    if not speed_mps or speed_mps <= 0:
        return "N/A"
    secs_per_km = 1000 / speed_mps
    mins = int(secs_per_km // 60)
    secs = int(secs_per_km % 60)
    return f"{mins}:{secs:02d}"


def format_duration(seconds: float) -> str:
    """Convert seconds to H:MM:SS or MM:SS."""
    if not seconds:
        return "N/A"
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def format_splits(splits_data) -> str:
    """Format per-km splits for the prompt."""
    if not splits_data:
        return "No split data available"
    if isinstance(splits_data, str):
        try:
            splits_data = json.loads(splits_data)
        except (json.JSONDecodeError, TypeError):
            return "No split data available"

    # Handle the Garmin splits format
    split_list = splits_data
    if isinstance(splits_data, dict):
        split_list = splits_data.get("lapDTOs", splits_data.get("splitDTOs", []))

    lines = []
    for i, split in enumerate(split_list):
        if isinstance(split, dict):
            dist = split.get("distance", 0) / 1000
            duration = split.get("duration", 0)
            avg_hr = split.get("averageHR", "")
            pace = format_pace(split.get("averageSpeed", 0))
            hr_str = f" | HR {avg_hr}" if avg_hr else ""
            lines.append(f"  Km {i+1}: {pace}/km{hr_str}")
    return "\n".join(lines) if lines else "No split data available"


def format_recent_activities(activities: list[dict]) -> str:
    """Format recent activities for context."""
    if not activities:
        return "No recent activities"
    lines = []
    for a in activities[:5]:
        lines.append(
            f"  {a.get('start_time', '?')}: "
            f"{a.get('distance_km', 0):.1f}km in {format_duration(a.get('duration_seconds', 0))} "
            f"({a.get('avg_pace_min_km', 'N/A')}/km) | HR {a.get('avg_hr', 'N/A')}"
        )
    return "\n".join(lines)


def _splits_list(splits_data) -> list[dict]:
    """Normalize splits_json (str or dict or list) into a flat list of split dicts."""
    if not splits_data:
        return []
    if isinstance(splits_data, str):
        try:
            splits_data = json.loads(splits_data)
        except (json.JSONDecodeError, TypeError):
            return []
    if isinstance(splits_data, dict):
        return splits_data.get("lapDTOs", splits_data.get("splitDTOs", []))
    if isinstance(splits_data, list):
        return splits_data
    return []


def compute_hr_drift(splits_data) -> dict | None:
    """Compute HR / pace decoupling between first and second halves of a run.

    Returns dict with first/second-half avg pace + HR and a decoupling % where:
        decoupling = ((HR2/Pace2) - (HR1/Pace1)) / (HR1/Pace1) * 100
    A positive % means HR rose faster than pace fell — typical aerobic-decoupling signal.
    Returns None if splits are too short to halve meaningfully.
    """
    splits = _splits_list(splits_data)
    splits = [s for s in splits if isinstance(s, dict) and s.get("averageHR") and s.get("averageSpeed")]
    if len(splits) < 4:  # need at least 2 per half for stability
        return None

    mid = len(splits) // 2
    first, second = splits[:mid], splits[mid:]

    def avg(items, key):
        vals = [it.get(key) for it in items if it.get(key)]
        return sum(vals) / len(vals) if vals else None

    hr1, hr2 = avg(first, "averageHR"), avg(second, "averageHR")
    spd1, spd2 = avg(first, "averageSpeed"), avg(second, "averageSpeed")
    if not all([hr1, hr2, spd1, spd2]):
        return None

    # Pace = 1/speed; "HR per unit pace" = HR * speed (since faster speed = lower pace)
    # We compute beats-per-meter: HR / speed → smaller number = more efficient
    bpm_per_speed_1 = hr1 / spd1
    bpm_per_speed_2 = hr2 / spd2
    decoupling_pct = (bpm_per_speed_2 - bpm_per_speed_1) / bpm_per_speed_1 * 100

    return {
        "first_half_pace": format_pace(spd1),
        "second_half_pace": format_pace(spd2),
        "first_half_hr": round(hr1),
        "second_half_hr": round(hr2),
        "decoupling_pct": round(decoupling_pct, 1),
    }


def compute_zone_distribution(hr_zones_json, splits_data, z2_min: int, z2_max: int) -> dict | None:
    """Return % time in Z1 / Z2 / Z3+ and combined easy (Z1+Z2).

    For easy-run quality, "easy time" = Z1 + Z2 — Z1 (recovery) is *easier* than
    Z2, so counting only Z2 against an 80% target is wrong (it penalises runs
    that started slow). The breakdown also lets the model spot Z3+ creep on a
    run that was prescribed easy.

    Prefers Garmin's `get_activity_hr_in_timezones` payload (zone-bucketed seconds);
    falls back to per-km splits with average HR if zones aren't available.
    """
    # Path 1: Garmin's zone breakdown
    if hr_zones_json:
        try:
            zones = json.loads(hr_zones_json) if isinstance(hr_zones_json, str) else hr_zones_json
        except (json.JSONDecodeError, TypeError):
            zones = None
        if isinstance(zones, list) and zones:
            total = sum(z.get("secsInZone", 0) for z in zones if isinstance(z, dict))
            if total > 0:
                def secs_for(n: int) -> float:
                    return sum(
                        z.get("secsInZone", 0)
                        for z in zones
                        if isinstance(z, dict) and z.get("zoneNumber") == n
                    )
                z1 = secs_for(1)
                z2 = secs_for(2)
                z3plus = total - z1 - z2
                return {
                    "z1_pct": round(z1 / total * 100, 1),
                    "z2_pct": round(z2 / total * 100, 1),
                    "z3plus_pct": round(max(z3plus, 0) / total * 100, 1),
                    "easy_pct": round((z1 + z2) / total * 100, 1),
                    "source": "garmin_zones",
                }

    # Path 2: derive from per-km splits using absolute Z2 BPM bounds
    splits = _splits_list(splits_data)
    z1_secs = z2_secs = z3_secs = 0.0
    total = 0.0
    for s in splits:
        if not isinstance(s, dict):
            continue
        hr = s.get("averageHR")
        dur = s.get("duration") or 0
        if not hr or not dur:
            continue
        total += dur
        if hr < z2_min:
            z1_secs += dur
        elif hr <= z2_max:
            z2_secs += dur
        else:
            z3_secs += dur
    if total == 0:
        return None
    return {
        "z1_pct": round(z1_secs / total * 100, 1),
        "z2_pct": round(z2_secs / total * 100, 1),
        "z3plus_pct": round(z3_secs / total * 100, 1),
        "easy_pct": round((z1_secs + z2_secs) / total * 100, 1),
        "source": "splits_fallback",
    }


def compute_acr(db: Database, today: date) -> dict | None:
    """Acute:chronic load ratio. Acute = last 7 days, chronic = last 28 days.
    Returns None if there's no training-load data in the chronic window."""
    acute_start = (today - timedelta(days=7)).isoformat()
    chronic_start = (today - timedelta(days=28)).isoformat()
    end = (today + timedelta(days=1)).isoformat()  # exclusive upper bound

    acute = db.get_training_load_sum(acute_start, end)
    chronic = db.get_training_load_sum(chronic_start, end)
    if chronic <= 0:
        return None
    # Convert chronic total to a 7-day equivalent average for comparable units
    chronic_weekly = chronic / 4
    ratio = acute / chronic_weekly if chronic_weekly > 0 else None
    return {
        "acute_7d": round(acute, 1),
        "chronic_28d": round(chronic, 1),
        "ratio": round(ratio, 2) if ratio is not None else None,
    }


def compute_mileage_delta(db: Database, today: date) -> dict:
    """Trailing-7-day km vs the 28-day weekly average. Returns absolute and % delta.

    This is the distance-based analogue of ACR and deliberately uses the *same*
    rolling windows as compute_acr (acute = trailing 7d, chronic = trailing 28d
    / 4). Mirroring the windows is the whole point: a calendar week-to-date vs
    full-prior-week comparison is biased low mid-week (only N of 7 days banked),
    which previously made volume read "down" while ACR read "up" — a contradiction
    that was a pure window artifact, not a real signal. With matched windows the
    two can only ever agree in direction. Calendar-week *plan progress* lives in
    compute_weekly_target, which is correctly Monday-anchored — don't conflate them.
    """
    acute_start = (today - timedelta(days=7)).isoformat()
    chronic_start = (today - timedelta(days=28)).isoformat()
    end = (today + timedelta(days=1)).isoformat()  # exclusive upper bound, matches compute_acr

    last_7d = db.get_distance_sum(acute_start, end)
    chronic_28d = db.get_distance_sum(chronic_start, end)
    prior_weekly_avg = chronic_28d / 4
    pct_delta = ((last_7d - prior_weekly_avg) / prior_weekly_avg * 100) if prior_weekly_avg > 0 else None
    return {
        "last_7d_km": round(last_7d, 1),
        "prior_4wk_avg_km": round(prior_weekly_avg, 1),
        "pct_delta": round(pct_delta, 1) if pct_delta is not None else None,
    }


def compute_adherence(plan: TrainingPlan, db: Database, today: date, lookback_runs: int = 10) -> dict:
    """How many of the last `lookback_runs` prescribed runs did the runner actually complete?

    Walks backwards from yesterday over plan-prescribed run days (Tue/Thu/Sat),
    skipping rest/cross-training days. For each prescribed run date, looks for an
    activity in the DB that day. Returns counts and missed dates.
    """
    completed = 0
    missed_dates: list[str] = []
    seen = 0
    cursor = today - timedelta(days=1)
    safety_limit = lookback_runs * 7  # avoid infinite walk if plan is empty
    while seen < lookback_runs and safety_limit > 0:
        prescribed = plan.get_prescribed_run(cursor)
        if prescribed and prescribed.workout_type != "rest":
            seen += 1
            day_start = cursor.isoformat()
            day_end = (cursor + timedelta(days=1)).isoformat()
            acts = db.get_activities_for_range(day_start, day_end)
            if acts:
                completed += 1
            else:
                missed_dates.append(cursor.isoformat())
        cursor -= timedelta(days=1)
        safety_limit -= 1
    return {"completed": completed, "total": seen, "missed_dates": missed_dates}


def compute_easy_run_trend(plan: TrainingPlan, recent_runs: list[dict], n: int = 4) -> dict | None:
    """Compare the most recent N easy-prescribed runs on the metrics that matter for fitness.

    For each recent run, look up what was prescribed that day. If it was "easy", include it.
    Trend signals (lower=fitter for HR-at-pace; lower=better aerobic for drift):
      - avg pace
      - avg HR
      - avg HR/pace ratio (HR per m/s — proxy for "HR at this pace"; declining = fitter)
      - avg HR drift %
    Returns None if fewer than 2 easy runs are available (no trend without 2+).
    """
    easy_runs: list[dict] = []
    for run in recent_runs:
        st = run.get("start_time", "")
        if not st:
            continue
        try:
            d = date.fromisoformat(st[:10])
        except ValueError:
            continue
        prescribed = plan.get_prescribed_run(d)
        if prescribed and prescribed.workout_type == "easy":
            easy_runs.append(run)
        if len(easy_runs) >= n:
            break

    if len(easy_runs) < 2:
        return None

    # Order chronologically (oldest first)
    easy_runs = sorted(easy_runs, key=lambda r: r.get("start_time", ""))

    rows: list[dict] = []
    for r in easy_runs:
        avg_hr = r.get("avg_hr") or 0
        # avg_pace_min_km is stored as "M:SS" string; convert to seconds for the ratio
        pace_str = r.get("avg_pace_min_km") or ""
        pace_secs: float | None = None
        if isinstance(pace_str, str) and ":" in pace_str:
            try:
                m, s = pace_str.split(":")
                pace_secs = int(m) * 60 + int(s)
            except ValueError:
                pace_secs = None
        speed_mps = (1000 / pace_secs) if pace_secs else None
        hr_per_speed = (avg_hr / speed_mps) if (avg_hr and speed_mps) else None
        drift = compute_hr_drift(r.get("splits_json", ""))
        rows.append({
            "date": (r.get("start_time") or "")[:10],
            "distance_km": r.get("distance_km"),
            "pace": pace_str,
            "hr": avg_hr or None,
            "hr_per_speed": round(hr_per_speed, 2) if hr_per_speed else None,
            "drift_pct": drift["decoupling_pct"] if drift else None,
        })
    return {"runs": rows}


def compute_cadence_context(activity: dict, recent_runs: list[dict], n: int = 5) -> dict | None:
    """This run's cadence + running dynamics against the runner's OWN recent baseline.

    Baseline = mean cadence of up to `n` prior runs (the current run is excluded
    by start_time so it can't anchor its own baseline). This lets the coach say
    "down from your usual 176" instead of prescribing a generic target spm.
    Running dynamics (ground contact, step length, vertical oscillation) are
    passed through in Garmin's native units; the prompt supplies the reference
    bands so the model interprets rather than invents them.

    Returns None if the current run has no cadence to anchor on.
    """
    current = activity.get("avg_cadence")
    if not current:
        return None
    cur_start = activity.get("start_time") or ""
    prior = [
        r.get("avg_cadence")
        for r in recent_runs
        if r.get("avg_cadence") and r.get("start_time") != cur_start
    ][:n]
    baseline = round(sum(prior) / len(prior), 1) if prior else None
    return {
        "current": round(current, 1),
        "baseline": baseline,
        "n_baseline": len(prior),
        "delta": round(current - baseline, 1) if baseline is not None else None,
        "ground_contact_ms": activity.get("ground_contact_ms"),
        "stride_length_cm": activity.get("stride_length_cm"),
        "vertical_oscillation_cm": activity.get("vertical_oscillation_cm"),
    }


def format_cadence_context(ctx: dict | None) -> str:
    """One-line cadence-vs-baseline + running-dynamics summary for the prompt.

    Includes reference bands inline so the model judges form against real ranges
    instead of a stock 175–178 target. Omits any metric the device didn't record.
    """
    if not ctx:
        return "Cadence not recorded"
    parts = [f"Cadence {ctx['current']} spm"]
    if ctx["baseline"] is not None:
        d = ctx["delta"]
        parts.append(
            f"vs your recent baseline {ctx['baseline']} spm over {ctx['n_baseline']} runs "
            f"({'+' if d >= 0 else ''}{d})"
        )
    else:
        parts.append("(no prior baseline yet)")
    gct = ctx.get("ground_contact_ms")
    if gct:
        parts.append(f"ground contact {float(gct):.0f}ms [typical 200–300, lower=less braking]")
    vo = ctx.get("vertical_oscillation_cm")
    if vo:
        parts.append(f"vertical oscillation {float(vo):.1f}cm [typical 6–13, lower=less bounce]")
    sl = ctx.get("stride_length_cm")
    if sl:
        parts.append(f"step length {float(sl):.0f}cm")
    return " | ".join(parts)


def compute_weekly_target(plan: TrainingPlan, db: Database, today: date) -> dict | None:
    """Where the runner stands against this week's prescribed mileage target.

    Includes the planned runs still remaining this week (count + km), so callers
    can report "2 runs left totaling 16km" instead of "6 calendar days left",
    which would invite a misleading per-day average across rest days.

    Returns None if today is outside the plan window.
    """
    week = plan.get_week_for_date(today)
    if not week or not week.weekly_km_target:
        return None
    week_start = (today - timedelta(days=today.weekday())).isoformat()
    today_end = (today + timedelta(days=1)).isoformat()
    actual = db.get_distance_sum(week_start, today_end)
    pct = round(actual / week.weekly_km_target * 100, 1) if week.weekly_km_target > 0 else 0
    days_remaining = 6 - today.weekday()

    remaining_runs: list[dict] = []
    for offset in range(1, days_remaining + 1):
        d = today + timedelta(days=offset)
        prescribed = plan.get_prescribed_run(d)
        if prescribed and prescribed.distance_km > 0:
            remaining_runs.append({
                "weekday": d.strftime("%a"),
                "type": prescribed.workout_type,
                "km": round(prescribed.distance_km, 1),
            })
    remaining_km = round(sum(r["km"] for r in remaining_runs), 1)

    return {
        "actual_km": round(actual, 1),
        "target_km": round(week.weekly_km_target, 1),
        "pct": pct,
        "days_remaining": days_remaining,
        "remaining_runs_count": len(remaining_runs),
        "remaining_runs_km": remaining_km,
        "remaining_runs": remaining_runs,
        "week_number": week.week_number,
        "phase": week.phase,
    }


def _format_weekly_target(target: dict | None) -> str:
    """One-line weekly-target summary for LLM prompts.

    Frames "what's left" in terms of *prescribed runs*, not calendar days, so
    the model doesn't divide remaining km by 6 and suggest a daily average —
    the plan has rest days and the runner doesn't run every day.
    """
    if not target:
        return "Outside training plan window"
    base = (
        f"Wk {target['week_number']} ({target['phase']}): "
        f"{target['actual_km']}km of {target['target_km']}km target ({target['pct']}%)"
    )
    runs = target.get("remaining_runs") or []
    if not runs:
        return base + ". No more runs scheduled this week."
    breakdown = ", ".join(f"{r['weekday']} {r['type']} {r['km']}km" for r in runs)
    return (
        base
        + f". {target['remaining_runs_count']} prescribed run(s) left "
        f"({target['remaining_runs_km']}km total): {breakdown}."
    )


def format_upcoming_runs(plan: TrainingPlan, today: date, days: int = 3) -> str:
    """Return the next `days` days of prescribed running, one line each. Skips rest days."""
    lines: list[str] = []
    for offset in range(days):
        d = today + timedelta(days=offset)
        prescribed = plan.get_prescribed_run(d)
        label = d.strftime("%a %b %d")
        if not prescribed or prescribed.workout_type == "rest":
            lines.append(f"  {label}: rest / cross-training")
        else:
            lines.append(f"  {label}: {prescribed.description}")
    return "\n".join(lines) if lines else "No upcoming prescription"


_FEEL_LABELS = {
    0: "Very Weak",
    25: "Weak",
    50: "Normal",
    75: "Strong",
    100: "Very Strong",
}


def format_feel(feel: int | float | None) -> str | None:
    """Map Garmin's 0–100 feel score (step 25) to its on-watch label."""
    if feel is None:
        return None
    try:
        f = int(round(float(feel) / 25.0) * 25)
    except (TypeError, ValueError):
        return None
    return _FEEL_LABELS.get(f, f"Score {feel}")


def format_weather(activity: dict) -> str:
    """One-line weather summary for the prompt. Returns 'Not recorded' for indoor runs."""
    temp = activity.get("temp_c")
    if temp is None and not activity.get("weather_label"):
        return "Not recorded (likely indoor / treadmill)"
    parts: list[str] = []
    if temp is not None:
        try:
            parts.append(f"{float(temp):.0f}°C")
        except (TypeError, ValueError):
            pass
    apparent = activity.get("apparent_temp_c")
    if apparent is not None:
        try:
            ap = float(apparent)
            # Skip if it's identical to actual temp — adds no signal
            if temp is None or abs(ap - float(temp)) >= 1:
                parts.append(f"feels {ap:.0f}°C")
        except (TypeError, ValueError):
            pass
    humidity = activity.get("humidity_pct")
    if humidity is not None:
        try:
            parts.append(f"{float(humidity):.0f}% humidity")
        except (TypeError, ValueError):
            pass
    wind = activity.get("wind_kph")
    if wind is not None:
        try:
            parts.append(f"wind {float(wind):.0f} kph")
        except (TypeError, ValueError):
            pass
    label = activity.get("weather_label")
    if label:
        parts.append(str(label))
    return " | ".join(parts) if parts else "Not recorded"


def heat_note(activity: dict) -> str:
    """Inline cue for the model when conditions distort HR-based reads."""
    temp = activity.get("temp_c")
    humidity = activity.get("humidity_pct")
    try:
        t = float(temp) if temp is not None else None
        h = float(humidity) if humidity is not None else None
    except (TypeError, ValueError):
        return ""
    if t is not None and t >= 25:
        return " — heat-adjusted read: discount HR drift, expect higher HR at same effort"
    if t is not None and t >= 20 and h is not None and h >= 70:
        return " — warm + humid: discount some HR drift"
    return ""


def format_recovery(wellness: dict | None, today: date | None = None) -> str:
    """Format the latest wellness row for the coach prompt.

    When `today` is given and the wellness row is >1 day behind, an explicit
    staleness flag is appended so the model knows to discount it — without
    that signal it would anchor on potentially-irrelevant HRV / sleep readings
    from a missed watch night.
    """
    if not wellness:
        return "No recent wellness data"
    parts = []
    if wellness.get("sleep_seconds"):
        h = wellness["sleep_seconds"] / 3600
        parts.append(f"Sleep: {h:.1f}h")
    if wellness.get("sleep_score") is not None:
        parts.append(f"Score: {wellness['sleep_score']}")
    if wellness.get("hrv_last_night") is not None:
        parts.append(f"HRV: {wellness['hrv_last_night']}ms")
        if wellness.get("hrv_7d_avg") is not None:
            parts.append(f"(7d avg {wellness['hrv_7d_avg']}ms")
            if wellness.get("hrv_status"):
                parts[-1] += f", {wellness['hrv_status']})"
            else:
                parts[-1] += ")"
    if wellness.get("rhr") is not None:
        parts.append(f"RHR: {wellness['rhr']} bpm")

    if not parts:
        return "No recent wellness data"

    staleness = _wellness_staleness(wellness, today)
    if staleness:
        parts.append(staleness)
    return " | ".join(parts)


def _wellness_staleness(wellness: dict, today: date | None) -> str | None:
    """Return a '(stale, N days old)' tag when the wellness row is >1 day behind today."""
    if today is None:
        return None
    raw = wellness.get("date")
    if not raw:
        return None
    try:
        wellness_date = date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None
    age = (today - wellness_date).days
    if age <= 1:  # yesterday's data is fresh — Garmin records sleep after waking
        return None
    return f"⚠️ stale: {age} days old — discount weight if today's state feels different"


class Coach:
    def __init__(self, api_key: str, plan: TrainingPlan, db: Database):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.plan = plan
        self.db = db

    def _build_system_prompt(self) -> str:
        today, tz_label = resolve_runner_today(self.db)
        week = self.plan.get_week_for_date(today)
        week_info = (
            f"Week {week.week_number} ({week.phase}), {week.start_date} to {week.end_date}"
            if week else "Outside training plan period"
        )

        pace_zones = "\n".join(
            f"  {pz.run_type}: {pz.pace} | {pz.hr_zone} | {pz.feel}"
            for pz in self.plan.pace_zones
        )
        benchmarks = "\n".join(
            f"  {b.distance}: {b.target_time} ({b.target_pace}) by {b.when_to_test}"
            for b in self.plan.benchmarks
        )

        # Lactate threshold from latest training-status snapshot, if any
        ts = self.db.get_latest_training_status()
        lt_line = ""
        if ts and (ts.get("lt_pace_min_km") or ts.get("lt_hr")):
            lt_pace = ts.get("lt_pace_min_km")
            lt_hr = ts.get("lt_hr")
            lt_pace_str = f"{int(lt_pace)}:{int(round((lt_pace - int(lt_pace)) * 60)):02d}/km" if lt_pace else "N/A"
            lt_line = f"\nLACTATE THRESHOLD (Garmin estimate): pace {lt_pace_str}, HR {lt_hr} bpm\n"

        latest_wellness = self.db.get_latest_wellness()
        rhr = latest_wellness.get("rhr") if latest_wellness else None
        z2_bounds = self.plan.get_z2_bounds(MAX_HR, rhr)
        if z2_bounds:
            method = f"Karvonen / %HRR with max HR {MAX_HR} (220−age) and RHR {rhr}" if rhr else f"%MaxHR with max HR {MAX_HR} (220−age) — RHR unavailable"
            z2_line = f"Z2 bounds ({method}, ±10 bpm typical error): {z2_bounds[0]}–{z2_bounds[1]} bpm"
        else:
            z2_line = ""

        total_plan_weeks = len(self.plan.weeks)
        return f"""You are a knowledgeable and encouraging running coach for a runner training for the Lisbon Marathon on October 10, 2026, targeting a sub-4:00 finish (3:57:57).

TRAINING PLAN:
- {total_plan_weeks}-week plan (v5), phases: Adaptation → Restart → Base Building → Specific Prep → Taper.
- Runs, rest days, and strength sessions vary by week — always check the prescribed cell for the day rather than assuming a fixed weekly pattern.
- Runner timezone: {tz_label} (today is {today.isoformat()} in the runner's local time — use this, not your assumed location, for season/weather context)
- Current training week: {week_info}

PACE ZONES:
{pace_zones}
{z2_line}{lt_line}

BENCHMARKS (target by August):
{benchmarks}

COACHING STYLE:
- Be encouraging but honest
- Flag potential injury risks (HR drift, pace inconsistency, overtraining)
- Keep messages concise and Telegram-friendly (under 2000 chars for run analysis)
- Use specific numbers from their data
- Compare actual vs prescribed when relevant
- Suggest adjustments only when data warrants it
- Use markdown formatting sparingly (bold for emphasis only)"""

    def analyze_run(self, activity: dict) -> str:
        """Analyze a completed run against the training plan."""
        start_time = activity.get("start_time", "")
        run_date = date.fromisoformat(start_time[:10]) if start_time else date.today()
        prescribed = self.plan.get_prescribed_run(run_date)
        week = self.plan.get_week_for_date(run_date)
        recent = self.db.get_recent_activities(limit=5)

        weekday_name = run_date.strftime("%A, %B %d")
        week_info = f"Week {week.week_number}, {week.phase}" if week else "unknown week"

        prescribed_text = (
            prescribed.description.replace("\n", " ")
            if prescribed
            else "No run prescribed (rest day or unscheduled run)"
        )

        # Training status context
        ts = self.db.get_latest_training_status()
        training_status_text = "Not available"
        if ts:
            training_status_text = (
                f"7-day load: {ts.get('training_load_7d', 'N/A')} | "
                f"Recovery: {ts.get('recovery_time_hours', 'N/A')}h | "
                f"VO2max: {ts.get('vo2max', 'N/A')} | "
                f"Status: {ts.get('training_status_label', 'N/A')}"
            )

        # Recovery & readiness from latest wellness row
        latest_wellness = self.db.get_latest_wellness()
        recovery_text = format_recovery(latest_wellness, run_date)
        rhr_for_zones = latest_wellness.get("rhr") if latest_wellness else None

        # Run-quality metrics
        drift = compute_hr_drift(activity.get("splits_json", ""))
        if drift:
            drift_text = (
                f"1st half {drift['first_half_pace']}/km @ {drift['first_half_hr']} bpm | "
                f"2nd half {drift['second_half_pace']}/km @ {drift['second_half_hr']} bpm | "
                f"decoupling {drift['decoupling_pct']}% "
                f"({'aerobic drift' if drift['decoupling_pct'] > 5 else 'stable'})"
            )
        else:
            drift_text = "Run too short to compute meaningfully"

        z2_bounds = self.plan.get_z2_bounds(MAX_HR, rhr_for_zones)
        zone_dist_text = "N/A"
        if z2_bounds:
            dist = compute_zone_distribution(
                activity.get("hr_zones_json"),
                activity.get("splits_json", ""),
                z2_bounds[0],
                z2_bounds[1],
            )
            if dist is not None:
                zone_dist_text = (
                    f"Easy (Z1+Z2) {dist['easy_pct']}% (target ≥80% on easy runs) — "
                    f"breakdown: Z1 {dist['z1_pct']}% | Z2 {dist['z2_pct']}% | "
                    f"Z3+ {dist['z3plus_pct']}% "
                    f"[Z2 band = {z2_bounds[0]}-{z2_bounds[1]} bpm via %HRR]"
                )

        # Load context
        acr = compute_acr(self.db, run_date)
        acr_text = (
            f"Acute:Chronic load ratio: {acr['ratio']} (acute 7d={acr['acute_7d']}, chronic 28d={acr['chronic_28d']}; "
            f"sweet spot 0.8–1.3, >1.5 = injury risk)"
            if acr else "ACR: insufficient training-load history"
        )
        delta = compute_mileage_delta(self.db, run_date)
        delta_pct = delta.get("pct_delta")
        delta_text = (
            f"Last 7 days {delta['last_7d_km']}km vs 4-week avg {delta['prior_4wk_avg_km']}km/wk "
            f"({'+' if (delta_pct or 0) >= 0 else ''}{delta_pct}%) — trailing window, "
            f"tracks ACR; not calendar-week progress"
            if delta_pct is not None
            else f"Last 7 days {delta['last_7d_km']}km (no prior baseline)"
        )

        # Plan adherence and weekly target progress
        adherence = compute_adherence(self.plan, self.db, run_date)
        if adherence["total"] > 0:
            adherence_text = (
                f"Completed {adherence['completed']}/{adherence['total']} of last "
                f"{adherence['total']} prescribed runs"
            )
            if adherence["missed_dates"]:
                adherence_text += f" (missed: {', '.join(adherence['missed_dates'][:3])})"
        else:
            adherence_text = "No prescribed runs in lookback window"

        target = compute_weekly_target(self.plan, self.db, run_date)
        weekly_target_text = _format_weekly_target(target)

        # Cross-run trend on prescribed easy runs (last 4 like-for-like)
        trend = compute_easy_run_trend(self.plan, recent, n=4)
        if trend and len(trend["runs"]) >= 2:
            trend_lines = []
            for r in trend["runs"]:
                trend_lines.append(
                    f"  {r['date']}: {r['distance_km']}km @ {r['pace']}/km, "
                    f"HR {r['hr']} ({r['hr_per_speed']} bpm·s/m), drift {r['drift_pct']}%"
                )
            trend_text = (
                "Last easy runs (chronological, oldest first):\n"
                + "\n".join(trend_lines)
                + "\n  → declining HR-per-speed = aerobic fitness improving; "
                "rising drift% = same-pace effort costing more"
            )
        else:
            trend_text = "Not enough easy-run history yet for a trend (need 2+)"

        # Conditions + subjective effort (from the watch's post-run prompt)
        weather_text = format_weather(activity)
        heat_cue = heat_note(activity)
        rpe = activity.get("rpe")
        feel = activity.get("feel")
        feel_label = format_feel(feel)
        if rpe is None and feel_label is None:
            subjective_text = "Not logged (no watch prompt response)"
        else:
            bits = []
            if rpe is not None:
                bits.append(f"RPE {rpe}/10")
            if feel_label is not None:
                bits.append(f"Feel: {feel_label}")
            subjective_text = " | ".join(bits)

        # Cadence + running dynamics against the runner's own recent baseline
        cadence_text = format_cadence_context(compute_cadence_context(activity, recent))

        user_prompt = f"""Analyze this run and provide coaching feedback.

TODAY'S RUN ({weekday_name}):
- Distance: {activity.get('distance_km', 0):.2f} km
- Duration: {format_duration(activity.get('duration_seconds', 0))}
- Avg Pace: {activity.get('avg_pace_min_km', 'N/A')}/km
- Avg HR: {activity.get('avg_hr', 'N/A')} bpm
- Max HR: {activity.get('max_hr', 'N/A')} bpm
- Calories: {activity.get('calories', 'N/A')}
- Avg Cadence: {activity.get('avg_cadence', 'N/A')} spm
- Elevation: +{activity.get('elevation_gain', 'N/A')}m / -{activity.get('elevation_loss', 'N/A')}m
- Aerobic TE: {activity.get('aerobic_te', 'N/A')} | Anaerobic TE: {activity.get('anaerobic_te', 'N/A')}
- Training Load: {activity.get('training_load', 'N/A')}
- Garmin Assessment: {activity.get('training_effect_label', 'N/A')}

CONDITIONS:
- Weather: {weather_text}{heat_cue}

SUBJECTIVE EFFORT (from watch post-run prompt):
- {subjective_text}

RUN QUALITY:
- HR Drift: {drift_text}
- Zone Distribution: {zone_dist_text}

RUNNING FORM:
- {cadence_text}

LOAD CONTEXT:
- {acr_text}
- {delta_text}
- Adherence: {adherence_text}
- Weekly target: {weekly_target_text}

EASY-RUN TREND:
{trend_text}

RECOVERY & READINESS (last available night):
{recovery_text}

TRAINING STATUS:
{training_status_text}

SPLITS:
{format_splits(activity.get('splits_json', ''))}

PRESCRIBED FOR TODAY ({week_info}):
{prescribed_text}

RECENT TRAINING:
{format_recent_activities(recent)}

Write the review using EXACTLY these sections, in this order, each under the bold
header shown (with its leading emoji) — don't rename, reorder, merge, or add
sections, and don't change a section's emoji. The ALWAYS sections appear every
time. The [only if] sections are report-by-exception: include them only when
their trigger is met and OMIT them entirely otherwise (don't echo data the runner
already logged or restate a tally that isn't actionable).

🎯 **Verdict** [always] — one line (e.g. "Solid easy run, right on target").

📋 **Prescribed vs Actual** [always] — compare distance and pace to what was prescribed.

❤️ **HR & Effort** [always] — use HR drift % and the zone breakdown. Easy-run target is Z1+Z2 ≥80% (Z1 recovery counts as easy, not "too slow"); only flag "ran too hard" if Z3+ is meaningfully elevated (>15% on an easy run). If temp ≥25°C (or ≥20°C + ≥70% humidity), discount HR drift — same effort shows higher HR in heat, not lost fitness.

💬 **Subjective** [only if RPE/Feel diverges] — high RPE (≥7) or "Weak"/"Very Weak" feel WITH normal HR/pace (early fatigue/illness — flag it), low RPE (≤4) on a hard prescribed session (you had more to give), or a multi-run drift toward feeling worse. If RPE/Feel simply matches the run, OMIT — don't read the logged number back.

📈 **Trend** [always] — if the easy-run trend shows HR-per-speed declining or drift% falling across runs, call out the fitness gain; if rising, flag it. If there isn't enough history yet, say so in one line.

🔋 **Recovery & Load** [always] — first ALWAYS state the latest night's sleep score, HRV (with its 7-day avg), and RHR, even when nominal; if the wellness data is flagged stale, say so and discount it. Then the load read: ACR and the trailing-7d volume delta share the same rolling window, so read them as ONE signal — never present them as a contradiction. Weigh WHY ACR is high: after a layoff the 28-day chronic base is depressed, so a high ratio can be a baseline artifact at low absolute volume — distinguish that from genuine ramping (rising absolute trailing-7d km), the real injury risk during a rebuild. Flag concerns: ACR >1.5, trailing-7d volume jump >10%, adherence <70%, or poor HRV/sleep.

📅 **Weekly Target** [only if off track] — prescribed runs missed, or the runs still scheduled this week can't realistically close the gap. If on track, OMIT. Frame what's left as *prescribed runs remaining*, never a per-day average across calendar days (rest days exist; the runner doesn't run every day).

🏃 **Form** [only if it deviates] — cadence moved ≥3 spm from the runner's OWN recent baseline (in RUNNING FORM), framed as "down/up from your usual N" (never a generic target), OR ground contact / vertical oscillation sits notably outside the typical band shown, OR elevation shaped the run. Otherwise OMIT.

💪 **Best** [always] — one thing done well.

👀 **Watch** [always] — one thing to watch or improve.

⏭️ **Next Up** [always] — brief look-ahead to the next scheduled run.

EMOJIS — keep them deterministic and accessible, not decorative:
- Each section header carries its fixed leading emoji above; never swap or add others to a header.
- Inside the text, use ONLY this status set, and only to mark a genuine judgement: ✅ on target / good, ⚠️ worth watching, 🔴 a flag that needs action. Use them sparingly — at most one or two per section, never stacked.
- Emojis must never replace words: every line has to read correctly with all emojis removed (they're scanning aids, not content).

FORMATTING: put each bold header (with its emoji) on its OWN line, content on the
next line(s), with a blank line between sections — never "**Header:** text…" inline."""

        response = self.client.messages.create(
            model=MODEL,
            max_tokens=4096,
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
            system=[
                {
                    "type": "text",
                    "text": self._build_system_prompt(),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_prompt}],
        )
        return _extract_text(response)

    def _race_countdown(self) -> dict:
        today, _ = resolve_runner_today(self.db)
        days_remaining = (RACE_DATE - today).days
        total_weeks = len(self.plan.weeks) or 1
        # Prefer the plan's own week numbering when today is in-plan; that way
        # "Week N/M" lines up with the xlsx even across non-uniform gaps (e.g.
        # the 3-week time-off span between Wk5 and Wk6 in v5).
        plan_week = self.plan.get_week_for_date(today)
        if plan_week:
            current_week = plan_week.week_number
        else:
            elapsed_weeks = (today - PLAN_START_DATE).days / 7
            current_week = min(max(int(elapsed_weeks) + 1, 1), total_weeks)
        pct_complete = min(current_week / total_weeks * 100, 100)
        weeks_remaining = max((RACE_DATE - today).days / 7, 0)
        return {
            "days_remaining": days_remaining,
            "current_week": current_week,
            "total_weeks": total_weeks,
            "pct_complete": round(pct_complete, 1),
            "weeks_remaining": round(weeks_remaining, 1),
        }

    def weekly_summary(self, week_start: str, week_end: str) -> str:
        activities = self.db.get_activities_for_range(week_start, week_end + "T23:59:59")
        countdown = self._race_countdown()
        run_date = date.fromisoformat(week_start)
        week = self.plan.get_week_for_date(run_date)

        # Summarize actual training
        total_km = sum(a.get("distance_km", 0) for a in activities)
        num_runs = len(activities)
        avg_paces = [a.get("avg_pace_min_km", "") for a in activities if a.get("avg_pace_min_km")]

        week_info = f"Week {week.week_number} ({week.phase})" if week else "Unknown week"
        prescribed_km = week.weekly_km_target if week else 0

        activities_text = "\n".join(
            f"  {a.get('start_time', '?')[:10]}: {a.get('distance_km', 0):.1f}km "
            f"at {a.get('avg_pace_min_km', 'N/A')}/km | HR {a.get('avg_hr', 'N/A')} | "
            f"Cadence {a.get('avg_cadence', 'N/A')} | "
            f"Elev +{a.get('elevation_gain', 'N/A')}m"
            for a in activities
        ) or "  No runs recorded"

        # Training status
        ts = self.db.get_latest_training_status()
        ts_text = "Not available"
        if ts:
            ts_text = (
                f"7-day load: {ts.get('training_load_7d', 'N/A')} | "
                f"Recovery: {ts.get('recovery_time_hours', 'N/A')}h | "
                f"VO2max: {ts.get('vo2max', 'N/A')} | "
                f"Status: {ts.get('training_status_label', 'N/A')}"
            )

        # Load + ramp context
        end_date_obj = date.fromisoformat(week_end)
        acr = compute_acr(self.db, end_date_obj)
        acr_text = (
            f"ACR {acr['ratio']} (acute 7d={acr['acute_7d']}, chronic 28d={acr['chronic_28d']}; sweet spot 0.8–1.3)"
            if acr else "ACR: insufficient history"
        )
        delta = compute_mileage_delta(self.db, end_date_obj)
        dpct = delta.get("pct_delta")
        delta_text = (
            f"Trailing 7d vs 4-wk avg: {delta['last_7d_km']}km vs {delta['prior_4wk_avg_km']}km/wk "
            f"({'+' if (dpct or 0) >= 0 else ''}{dpct}%)"
            if dpct is not None
            else f"Trailing 7d: {delta['last_7d_km']}km (no prior baseline)"
        )

        # Wellness trend across the week
        wellness_rows = self.db.get_wellness_for_range(week_start, week_end)
        wellness_text = "No wellness data this week"
        if wellness_rows:
            sleep_vals = [w["sleep_seconds"] for w in wellness_rows if w.get("sleep_seconds")]
            hrv_vals = [w["hrv_last_night"] for w in wellness_rows if w.get("hrv_last_night") is not None]
            rhr_vals = [w["rhr"] for w in wellness_rows if w.get("rhr") is not None]
            parts = []
            if sleep_vals:
                avg_sleep_h = sum(sleep_vals) / len(sleep_vals) / 3600
                parts.append(f"avg sleep {avg_sleep_h:.1f}h ({len(sleep_vals)} nights)")
            if hrv_vals:
                parts.append(f"HRV avg {sum(hrv_vals)/len(hrv_vals):.0f}ms (range {min(hrv_vals):.0f}–{max(hrv_vals):.0f})")
            if rhr_vals:
                parts.append(f"RHR avg {sum(rhr_vals)/len(rhr_vals):.0f} bpm (range {min(rhr_vals)}–{max(rhr_vals)})")
            wellness_text = " | ".join(parts) if parts else "No wellness data this week"

        user_prompt = f"""Generate a weekly training summary and review.

WEEK: {week_info} ({week_start} to {week_end})

RACE COUNTDOWN:
- Lisbon Marathon: {countdown['days_remaining']} days away
- Training progress: Week {countdown['current_week']}/{countdown['total_weeks']} ({countdown['pct_complete']}% complete)
- Weeks remaining: {countdown['weeks_remaining']}

ACTUAL TRAINING THIS WEEK:
- Total runs: {num_runs}
- Total distance: {total_km:.1f} km (prescribed: {prescribed_km} km)
{activities_text}

LOAD & RAMP:
- {acr_text}
- {delta_text}

WELLNESS TREND (overnight metrics):
{wellness_text}

TRAINING STATUS:
{ts_text}

PRESCRIBED THIS WEEK:
{self.plan.get_week_summary(week) if week else 'No plan data'}

Provide:
1. Week headline (e.g. "Strong week — hit all targets")
2. Volume comparison (actual vs prescribed km) and ramp call-out (>10% jump = caution)
3. Key observations from the runs (pace trends, HR patterns, cadence)
4. Load assessment using ACR (flag if outside 0.8–1.3). The trailing-7d volume delta shares ACR's window — read them together as one signal, never as a contradiction. After a layoff a depressed chronic base inflates ACR, so separate that artifact from genuine ramping (rising absolute trailing-7d km).
5. Recovery state from wellness trend (sleep avg, HRV/RHR drift)
6. What went well this week
7. Focus for next week
8. Race countdown motivation (mention days/weeks remaining)

Keep it Telegram-friendly (under 3000 chars)."""

        response = self.client.messages.create(
            model=MODEL,
            max_tokens=4096,
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
            system=[
                {
                    "type": "text",
                    "text": self._build_system_prompt(),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_prompt}],
        )
        return _extract_text(response)

    def morning_brief(self, today: date) -> str:
        """Generate a short pre-run advisory: stick with the plan, modify, or postpone.

        Pulls the same context analyze_run uses — wellness (with staleness flag),
        ACR, weekly progress, prescribed workout, upcoming runs — and asks the
        model for a one-line verdict + specific modification if warranted.
        Designed to fire at 06:00 runner-local, hours before the prescribed run.
        """
        prescribed = self.plan.get_prescribed_run(today)
        if not prescribed or prescribed.workout_type == "rest":
            # The caller (prerun.py) gates on this too, but defend in depth.
            return ""

        weekday_name = today.strftime("%A, %B %d")
        wellness = self.db.get_latest_wellness()
        recovery_text = format_recovery(wellness, today)
        acr = compute_acr(self.db, today)
        acr_text = (
            f"ACR {acr['ratio']} (acute 7d={acr['acute_7d']}, chronic 28d={acr['chronic_28d']}; "
            f"sweet spot 0.8–1.3, danger >1.5)"
            if acr else "ACR: insufficient training-load history"
        )
        target = compute_weekly_target(self.plan, self.db, today)
        weekly_target_text = _format_weekly_target(target)
        upcoming = format_upcoming_runs(self.plan, today, days=3)
        ts = self.db.get_latest_training_status()
        ts_text = "Not available"
        if ts:
            ts_text = (
                f"7-day load: {ts.get('training_load_7d', 'N/A')} | "
                f"Recovery: {ts.get('recovery_time_hours', 'N/A')}h | "
                f"Status: {ts.get('training_status_label', 'N/A')}"
            )

        prescribed_text = prescribed.description.replace("\n", " ")

        user_prompt = f"""It's early morning — runner hasn't trained yet today. Give a pre-run brief that helps them decide what to do this morning.

TODAY ({weekday_name}):
- Prescribed: {prescribed_text}

RECOVERY & READINESS (last available night):
{recovery_text}

LOAD CONTEXT:
- {acr_text}
- Weekly target: {weekly_target_text}

TRAINING STATUS:
{ts_text}

NEXT FEW DAYS:
{upcoming}

Provide:
1. **Verdict** in one short line — one of: stick with plan / modify / postpone / skip.
2. **Recommendation** if modifying — specific (e.g. "drop pace to easy 6:30/km", "swap with Saturday's easy run", "cut to 4km"). One sentence.
3. **Why** — one short sentence citing the specific data point that drove the call (e.g. "HRV down 18% from baseline").

If stale-wellness flag is present, lean toward "trust your legs this morning — recent data missing."
Keep the whole thing under 500 chars — runner is reading on phone half-awake. No greetings, no sign-off."""

        response = self.client.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=[
                {
                    "type": "text",
                    "text": self._build_system_prompt(),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_prompt}],
        )
        return _extract_text(response)

    def chat(self, user_message: str) -> str:
        """Handle interactive conversation via Telegram."""
        history = self.db.get_recent_conversations(limit=10)
        recent_runs = self.db.get_recent_activities(limit=5)
        today, _ = resolve_runner_today(self.db)

        messages = [{"role": h["role"], "content": h["content"]} for h in history]

        # Build a rich context prefix so chat() reasons against the same data
        # analyze_run sees: latest analysis, ACR, weekly target, adherence,
        # wellness, and upcoming prescription.
        context_lines: list[str] = []
        if recent_runs:
            context_lines.append("Recent runs:")
            context_lines.append(format_recent_activities(recent_runs))

            latest = recent_runs[0]
            latest_analysis = latest.get("coaching_response")
            if latest_analysis:
                snippet = latest_analysis if len(latest_analysis) <= 1500 else latest_analysis[:1500] + "…"
                context_lines.append(
                    f"\nLatest run analysis ({(latest.get('start_time') or '?')[:10]}):\n{snippet}"
                )

        acr = compute_acr(self.db, today)
        if acr and acr.get("ratio") is not None:
            context_lines.append(
                f"\nCurrent ACR: {acr['ratio']} (acute 7d={acr['acute_7d']}, chronic 28d={acr['chronic_28d']})"
            )

        target = compute_weekly_target(self.plan, self.db, today)
        if target:
            tail = (
                f"{target['remaining_runs_count']} runs left "
                f"({target['remaining_runs_km']}km)"
                if target.get("remaining_runs_count")
                else "no more runs scheduled"
            )
            context_lines.append(
                f"This week: {target['actual_km']}/{target['target_km']} km "
                f"({target['pct']}%, {tail})"
            )

        adherence = compute_adherence(self.plan, self.db, today)
        if adherence["total"] > 0:
            context_lines.append(
                f"Adherence: {adherence['completed']}/{adherence['total']} of last "
                f"{adherence['total']} prescribed runs completed"
            )

        wellness = self.db.get_latest_wellness()
        recovery = format_recovery(wellness, today)
        if recovery and recovery != "No recent wellness data":
            context_lines.append(f"Latest wellness: {recovery}")

        upcoming = format_upcoming_runs(self.plan, today, days=3)
        if upcoming:
            context_lines.append(f"\nUpcoming 3 days:\n{upcoming}")

        context_prefix = (
            "[Context for this conversation — use it implicitly, don't restate "
            "unless the user asks:\n"
            + "\n".join(context_lines)
            + "\n]\n\n"
            if context_lines else ""
        )

        messages.append({"role": "user", "content": context_prefix + user_message})

        self.db.save_conversation("user", user_message)

        response = self.client.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=[
                {
                    "type": "text",
                    "text": self._build_system_prompt(),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=messages,
        )

        reply = _extract_text(response)
        self.db.save_conversation("assistant", reply)
        return reply
