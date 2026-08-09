#!/usr/bin/env python3
"""Morning pre-run brief — fires hourly, gates to 06:00 runner-local on run days.

Why this design: systemd's OnCalendar fires in the *Pi's* timezone, but we want
to deliver this in the *runner's* timezone (auto-derived from their latest
activity's UTC offset). Solution: timer fires every hour, this module checks
the runner-local time and dedups via prerun_sent, so only one of those 24 hourly
firings actually sends a message on a given runner-local date.
"""

import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

from src.config import ANTHROPIC_API_KEY, DB_PATH, TRAINING_PLAN_PATH
from src.coach import Coach, completed_run_dates, resolve_runner_today, runner_local_now
from src.db import Database
from src.telegram_bot import send_coaching_message, send_error_alert
from src.training_plan import TrainingPlan

# Runner-local hour to deliver the brief at. The hourly timer means we have a
# 60-minute window; this picks the hour we actually fire on.
TARGET_LOCAL_HOUR = 6


def run_prerun(force: bool = False) -> int:
    """Send a morning brief if all gates pass. Returns 1 if sent, 0 if skipped.

    Gates (all must pass):
      1. Runner-local hour matches TARGET_LOCAL_HOUR (skipped when force=True).
      2. Today resolves to a run — either its own prescribed slot, or one
         carried over from a nearby day that's still outstanding this week.
      3. No brief already sent for this runner-local date.
    """
    db = Database(DB_PATH)
    try:
        local_now = runner_local_now(db)
        today, _ = resolve_runner_today(db)

        if not force and local_now.hour != TARGET_LOCAL_HOUR:
            log.info("Skipping: runner-local hour is %d, want %d", local_now.hour, TARGET_LOCAL_HOUR)
            return 0

        plan = TrainingPlan(str(TRAINING_PLAN_PATH))
        # Resolve rather than look up by weekday: on a rest day that still has
        # an unrun slot beside it, the brief is for the session the runner has
        # moved onto today (Saturday's long run being done on Sunday).
        week = plan.get_week_for_date(today)
        completed = completed_run_dates(db, week) if week else set()
        resolved = plan.resolve_run_for_date(today, completed)
        if not resolved:
            log.info("Skipping: %s is a rest / cross-training day", today.isoformat())
            return 0

        if db.prerun_sent_today(today.isoformat()):
            log.info("Skipping: brief already sent for %s", today.isoformat())
            return 0

        log.info(
            "Generating morning brief for %s (%s%s)",
            today.isoformat(),
            resolved.run.workout_type,
            f", {resolved.shift_note()}" if resolved.shifted else "",
        )
        coach = Coach(ANTHROPIC_API_KEY, plan, db)
        try:
            brief = coach.morning_brief(today, resolved)
        except Exception as e:
            log.exception("morning_brief generation failed")
            send_error_alert(f"Morning brief failed for {today.isoformat()}: {e!r}")
            return 0

        if not brief:
            log.warning("Empty brief — nothing to send")
            return 0

        # Tag the message so it's visually distinct from post-run analyses.
        header = f"☀️ Morning brief — {today.strftime('%A %b %d')}"
        if resolved.shifted:
            header += f" ({resolved.shift_note()})"
        message = f"{header}\n\n{brief}"
        send_coaching_message(message)
        db.save_prerun(today.isoformat(), brief)
        log.info("Morning brief delivered for %s", today.isoformat())
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    force = "--force" in sys.argv  # bypass the time gate for testing
    sys.exit(0 if run_prerun(force=force) >= 0 else 1)
