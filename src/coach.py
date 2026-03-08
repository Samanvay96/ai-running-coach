import json
import logging
from datetime import date, timedelta

import anthropic

from .config import RACE_DATE, PLAN_START_DATE
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
- Garmin Assessment: {activity.get('training_effect_label', 'N/A')}

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
3. HR/effort analysis
4. Cadence & elevation note (if relevant)
5. One thing done well
6. One thing to watch or improve
7. Brief look-ahead to next scheduled run"""

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

TRAINING STATUS:
{ts_text}

PRESCRIBED THIS WEEK:
{self.plan.get_week_summary(week) if week else 'No plan data'}

Provide:
1. Week headline (e.g. "Strong week — hit all targets")
2. Volume comparison (actual vs prescribed km)
3. Key observations from the runs (pace trends, HR patterns, cadence)
4. Training load assessment
5. What went well this week
6. Focus for next week
7. Race countdown motivation (mention days/weeks remaining)

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
