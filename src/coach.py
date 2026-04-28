import json
import logging
from datetime import date, timedelta

import anthropic

from .config import RACE_DATE, PLAN_START_DATE, MAX_HR
from .db import Database
from .training_plan import TrainingPlan

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-20250514"


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


def compute_z2_pct(hr_zones_json, splits_data, z2_min: int, z2_max: int) -> float | None:
    """Return % of run time spent in Zone 2.

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
            # Garmin zones are 1-indexed; "zone 2" lands on zoneNumber=2
            z2_secs = sum(
                z.get("secsInZone", 0)
                for z in zones
                if isinstance(z, dict) and z.get("zoneNumber") == 2
            )
            if total > 0:
                return round(z2_secs / total * 100, 1)

    # Path 2: derive from per-km splits using absolute Z2 BPM bounds
    splits = _splits_list(splits_data)
    in_zone = 0.0
    total = 0.0
    for s in splits:
        if not isinstance(s, dict):
            continue
        hr = s.get("averageHR")
        dur = s.get("duration") or 0
        if not hr or not dur:
            continue
        total += dur
        if z2_min <= hr <= z2_max:
            in_zone += dur
    if total == 0:
        return None
    return round(in_zone / total * 100, 1)


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
    """This week's km vs prior 4-week avg. Returns absolute and % delta."""
    this_week_start = (today - timedelta(days=today.weekday())).isoformat()  # Mon of current week
    this_week_end = (today + timedelta(days=1)).isoformat()
    prior_start = (date.fromisoformat(this_week_start) - timedelta(days=28)).isoformat()

    this_km = db.get_distance_sum(this_week_start, this_week_end)
    prior_km = db.get_distance_sum(prior_start, this_week_start)
    prior_weekly_avg = prior_km / 4
    pct_delta = ((this_km - prior_weekly_avg) / prior_weekly_avg * 100) if prior_weekly_avg > 0 else None
    return {
        "this_week_km": round(this_km, 1),
        "prior_4wk_avg_km": round(prior_weekly_avg, 1),
        "pct_delta": round(pct_delta, 1) if pct_delta is not None else None,
    }


def format_recovery(wellness: dict | None) -> str:
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
    return " | ".join(parts) if parts else "No recent wellness data"


class Coach:
    def __init__(self, api_key: str, plan: TrainingPlan, db: Database):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.plan = plan
        self.db = db

    def _build_system_prompt(self) -> str:
        week = self.plan.get_week_for_date(date.today())
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

        return f"""You are a knowledgeable and encouraging running coach for a runner training for the Lisbon Marathon on October 10, 2026, targeting a sub-4:00 finish (3:57:57).

TRAINING PLAN:
- 32-week plan starting March 2, 2026
- Phases: Adaptation (wk 1-8), Base Building (wk 9-18), Specific Prep (wk 19-28), Taper (wk 29-32)
- Running days: Tuesday, Thursday, Saturday (long run)
- Cross-training: F45 Mon/Wed (reducing to 1x/week from Phase 2)
- Current date: {date.today().isoformat()}
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
        recovery_text = format_recovery(latest_wellness)
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
        z2_pct_text = "N/A"
        if z2_bounds:
            z2_pct = compute_z2_pct(
                activity.get("hr_zones_json"),
                activity.get("splits_json", ""),
                z2_bounds[0],
                z2_bounds[1],
            )
            if z2_pct is not None:
                z2_pct_text = f"{z2_pct}% (target ≥80% on easy runs; Z2 = {z2_bounds[0]}-{z2_bounds[1]} bpm via %HRR)"

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
            f"This week {delta['this_week_km']}km vs prior 4-week avg {delta['prior_4wk_avg_km']}km "
            f"({'+' if (delta_pct or 0) >= 0 else ''}{delta_pct}%)"
            if delta_pct is not None
            else f"This week {delta['this_week_km']}km (no prior baseline)"
        )

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

RUN QUALITY:
- HR Drift: {drift_text}
- Z2 Time-in-Zone: {z2_pct_text}

LOAD CONTEXT:
- {acr_text}
- {delta_text}

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

Provide:
1. One-line verdict (e.g. "Solid easy run, right on target")
2. Prescribed vs actual comparison
3. HR/effort analysis — explicitly use HR drift % and Z2 time-in-zone (call out if easy ran too hard)
4. Recovery & load read — flag if ACR >1.5, mileage jump >10%, or HRV/sleep poor
5. Cadence & elevation note (if relevant)
6. One thing done well
7. One thing to watch or improve
8. Brief look-ahead to next scheduled run"""

        response = self.client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=self._build_system_prompt(),
            messages=[{"role": "user", "content": user_prompt}],
        )
        return response.content[0].text

    def _race_countdown(self) -> dict:
        today = date.today()
        days_remaining = (RACE_DATE - today).days
        total_weeks = 32
        elapsed_weeks = (today - PLAN_START_DATE).days / 7
        current_week = min(max(int(elapsed_weeks) + 1, 1), total_weeks)
        pct_complete = min(elapsed_weeks / total_weeks * 100, 100)
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
            f"Week vs prior 4-wk avg: {delta['this_week_km']}km vs {delta['prior_4wk_avg_km']}km "
            f"({'+' if (dpct or 0) >= 0 else ''}{dpct}%)"
            if dpct is not None
            else f"Week total: {delta['this_week_km']}km (no prior baseline)"
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
4. Load assessment using ACR (flag if outside 0.8–1.3)
5. Recovery state from wellness trend (sleep avg, HRV/RHR drift)
6. What went well this week
7. Focus for next week
8. Race countdown motivation (mention days/weeks remaining)

Keep it Telegram-friendly (under 3000 chars)."""

        response = self.client.messages.create(
            model=MODEL,
            max_tokens=1500,
            system=self._build_system_prompt(),
            messages=[{"role": "user", "content": user_prompt}],
        )
        return response.content[0].text

    def chat(self, user_message: str) -> str:
        """Handle interactive conversation via Telegram."""
        history = self.db.get_recent_conversations(limit=10)
        recent_runs = self.db.get_recent_activities(limit=5)

        messages = [{"role": h["role"], "content": h["content"]} for h in history]

        context_prefix = ""
        if recent_runs:
            context_prefix = f"[Recent runs for context:\n{format_recent_activities(recent_runs)}]\n\n"

        messages.append({"role": "user", "content": context_prefix + user_message})

        self.db.save_conversation("user", user_message)

        response = self.client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=self._build_system_prompt(),
            messages=messages,
        )

        reply = response.content[0].text
        self.db.save_conversation("assistant", reply)
        return reply
