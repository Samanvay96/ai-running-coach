"""One-shot backfill for daily_wellness.

Walks back N days from yesterday, calling Garmin's sleep/HRV/RHR endpoints
for each date and persisting what's available. Idempotent (INSERT OR REPLACE
keyed on date) — safe to re-run.

Usage:
    python -m src.backfill_wellness            # last 30 days
    python -m src.backfill_wellness 60         # last 60 days
"""

import json
import logging
import sys
import time
from datetime import date, timedelta

from .config import GARMIN_EMAIL, GARMIN_PASSWORD, DB_PATH
from .db import Database
from .garmin_client import GarminClient

log = logging.getLogger(__name__)


def _extract(garmin: GarminClient, target_date: date) -> dict | None:
    """Mirror poller.py's wellness extraction. Returns dict or None if no data at all."""
    sleep_data = garmin.get_sleep(target_date)
    hrv_data = garmin.get_hrv(target_date)
    rhr_data = garmin.get_rhr(target_date)

    sleep_seconds = None
    sleep_score = None
    if sleep_data:
        dto = sleep_data.get("dailySleepDTO") or {}
        sleep_seconds = dto.get("sleepTimeSeconds")
        scores = dto.get("sleepScores") or {}
        sleep_score = (scores.get("overall") or {}).get("value")

    hrv_last_night = None
    hrv_7d_avg = None
    hrv_status = None
    if hrv_data:
        summary = hrv_data.get("hrvSummary") or {}
        hrv_last_night = summary.get("lastNightAvg")
        hrv_7d_avg = summary.get("weeklyAvg")
        hrv_status = summary.get("status")

    rhr = None
    if rhr_data:
        metrics = rhr_data.get("allMetrics") or {}
        rhr_list = metrics.get("metricsMap", {}).get("WELLNESS_RESTING_HEART_RATE")
        if isinstance(rhr_list, list) and rhr_list:
            rhr = rhr_list[0].get("value")
        else:
            rhr = rhr_data.get("restingHeartRate")

    if all(v is None for v in [sleep_seconds, sleep_score, hrv_last_night, rhr]):
        return None

    return {
        "sleep_seconds": sleep_seconds,
        "sleep_score": sleep_score,
        "hrv_last_night": hrv_last_night,
        "hrv_7d_avg": hrv_7d_avg,
        "hrv_status": hrv_status,
        "rhr": rhr,
        "raw_json": json.dumps({"sleep": sleep_data, "hrv": hrv_data, "rhr": rhr_data}),
    }


def backfill(days: int = 30) -> tuple[int, int]:
    """Walk yesterday → yesterday-days+1 inclusive. Returns (filled, skipped)."""
    db = Database(DB_PATH)
    try:
        garmin = GarminClient(GARMIN_EMAIL, GARMIN_PASSWORD, db)
    except Exception as e:
        log.error("Garmin login failed: %s", e)
        db.close()
        return (0, 0)

    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    log.info("Backfilling daily_wellness from %s to %s (%d days)", start, end, days)

    filled = 0
    skipped = 0
    cur = start
    while cur <= end:
        try:
            data = _extract(garmin, cur)
        except Exception as e:
            log.warning("Extraction failed for %s: %s", cur, e)
            data = None

        if data:
            db.save_daily_wellness(
                target_date=cur.isoformat(),
                sleep_seconds=data["sleep_seconds"],
                sleep_score=data["sleep_score"],
                hrv_last_night=data["hrv_last_night"],
                hrv_7d_avg=data["hrv_7d_avg"],
                hrv_status=data["hrv_status"],
                rhr=data["rhr"],
                raw_json=data["raw_json"],
            )
            filled += 1
            log.info(
                "%s: sleep=%ss score=%s HRV=%s RHR=%s",
                cur, data["sleep_seconds"], data["sleep_score"],
                data["hrv_last_night"], data["rhr"],
            )
        else:
            skipped += 1
            log.info("%s: no wellness data", cur)

        cur += timedelta(days=1)
        time.sleep(0.3)  # gentle pacing on Garmin Connect

    db.close()
    log.info("Done. Filled %d days, skipped %d.", filled, skipped)
    return (filled, skipped)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    backfill(days=days)
