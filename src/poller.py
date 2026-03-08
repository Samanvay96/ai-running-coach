#!/usr/bin/env python3
"""Cron entry point: check for new Garmin activities, analyze, and send to Telegram."""

import json
import logging
import sys

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
from src.telegram_bot import send_coaching_message


def poll():
    db = Database(DB_PATH)
    try:
        garmin = GarminClient(GARMIN_EMAIL, GARMIN_PASSWORD, db)
    except Exception as e:
        log.error("Failed to connect to Garmin: %s", e)
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

    if new_count == 0:
        log.info("No new activities found.")
    else:
        log.info("Processed %d new activities.", new_count)

    db.close()


if __name__ == "__main__":
    poll()
