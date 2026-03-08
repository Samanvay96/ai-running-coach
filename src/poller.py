#!/usr/bin/env python3
"""Cron entry point: check for new Garmin activities, analyze, and send to Telegram."""

import json
import logging
import sys
from datetime import date, timedelta

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

from src.config import GARMIN_EMAIL, GARMIN_PASSWORD, ANTHROPIC_API_KEY, TRAINING_PLAN_PATH, DB_PATH
from src.db import Database
from src.garmin_client import GarminClient
from src.training_plan import TrainingPlan
from src.coach import Coach, format_pace
from src.telegram_bot import send_coaching_message, send_error_alert


def poll():
    db = Database(DB_PATH)
    try:
        garmin = GarminClient(GARMIN_EMAIL, GARMIN_PASSWORD, db)
    except Exception as e:
        log.error("Failed to connect to Garmin: %s", e)
        send_error_alert(f"Garmin login failed: {e}")
        db.close()
        return

    plan = TrainingPlan(str(TRAINING_PLAN_PATH))
    coach = Coach(ANTHROPIC_API_KEY, plan, db)

    activities = garmin.get_recent_activities(limit=5)
    new_count = 0

    for activity in activities:
        activity_id = activity["activityId"]

        if db.activity_exists(activity_id):
            continue

        log.info("New activity found: %s (ID: %s)", activity.get("activityName"), activity_id)

        # Fetch splits
        splits = garmin.get_activity_splits(activity_id)

        # Extract and store key metrics
        distance_km = (activity.get("distance") or 0) / 1000
        duration_seconds = activity.get("duration") or 0
        avg_speed = activity.get("averageSpeed") or 0
        avg_pace = format_pace(avg_speed)

        avg_cadence = activity.get("averageRunningCadenceInStepsPerMinute")
        elevation_gain = activity.get("elevationGain")
        elevation_loss = activity.get("elevationLoss")
        anaerobic_te = activity.get("anaerobicTrainingEffect")

        db.save_activity(
            activity_id=activity_id,
            start_time=activity.get("startTimeLocal", ""),
            activity_type=activity.get("activityType", {}).get("typeKey", ""),
            distance_km=distance_km,
            duration_seconds=duration_seconds,
            avg_pace=avg_pace,
            avg_hr=activity.get("averageHR"),
            max_hr=activity.get("maxHR"),
            calories=activity.get("calories"),
            aerobic_te=activity.get("aerobicTrainingEffect"),
            vo2max=activity.get("vO2MaxValue"),
            raw_json=json.dumps(activity),
            splits_json=json.dumps(splits),
            avg_cadence=avg_cadence,
            elevation_gain=elevation_gain,
            elevation_loss=elevation_loss,
            anaerobic_te=anaerobic_te,
        )

        # Build activity dict for coach (matches what coach.analyze_run expects)
        activity_for_coach = {
            "start_time": activity.get("startTimeLocal", ""),
            "distance_km": distance_km,
            "duration_seconds": duration_seconds,
            "avg_pace_min_km": avg_pace,
            "avg_hr": activity.get("averageHR"),
            "max_hr": activity.get("maxHR"),
            "calories": activity.get("calories"),
            "aerobic_te": activity.get("aerobicTrainingEffect"),
            "anaerobic_te": anaerobic_te,
            "avg_cadence": avg_cadence,
            "elevation_gain": elevation_gain,
            "elevation_loss": elevation_loss,
            "training_effect_label": activity.get("trainingEffectLabel"),
            "splits_json": json.dumps(splits),
        }

        try:
            coaching = coach.analyze_run(activity_for_coach)
            db.mark_processed(activity_id, coaching)
            send_coaching_message(coaching)
            log.info("Coaching sent for activity %s", activity_id)
            new_count += 1
        except Exception as e:
            log.error("Failed to analyze/send activity %s: %s", activity_id, e)
            send_error_alert(f"Failed to analyze activity {activity_id}: {e}")

    if new_count == 0:
        log.info("No new activities found.")
    else:
        log.info("Processed %d new activities.", new_count)

    # Fetch and store training status snapshot
    try:
        ts = garmin.get_training_status()
        if ts:
            db.save_training_status(
                snapshot_date=date.today().isoformat(),
                training_load_7d=ts.get("weeklyTrainingLoad"),
                recovery_time_hours=ts.get("recoveryTimeInHours"),
                vo2max=ts.get("vo2MaxValue"),
                training_status_label=ts.get("trainingStatusLabel"),
                raw_json=json.dumps(ts),
            )
            log.info("Training status snapshot saved")
    except Exception as e:
        log.warning("Failed to save training status: %s", e)

    # Weekly summary — trigger on Sunday
    today = date.today()
    if today.weekday() == 6:  # Sunday
        week_start = (today - timedelta(days=6)).isoformat()
        if not db.weekly_summary_sent(week_start):
            try:
                week_end = today.isoformat()
                summary = coach.weekly_summary(week_start, week_end)
                week = plan.get_week_for_date(today)
                week_num = week.week_number if week else 0
                db.save_weekly_summary(week_num, week_start, week_end, summary)
                send_coaching_message(summary)
                log.info("Weekly summary sent for week starting %s", week_start)
            except Exception as e:
                log.error("Failed to send weekly summary: %s", e)
                send_error_alert(f"Weekly summary failed: {e}")

    db.close()


if __name__ == "__main__":
    poll()
