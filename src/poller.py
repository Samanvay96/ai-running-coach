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
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

from src.config import GARMIN_EMAIL, GARMIN_PASSWORD, ANTHROPIC_API_KEY, TRAINING_PLAN_PATH, DB_PATH
from src.db import Database
from src.garmin_client import GarminClient
from src.training_plan import TrainingPlan
from src.coach import Coach, format_pace
from src.telegram_bot import send_coaching_message, send_error_alert, send_backup_to_telegram
from src.backup import run_backup
from src.time_utils import compute_tz_offset_minutes


def _f_to_c(f: float | int | None) -> float | None:
    if f is None:
        return None
    return round((float(f) - 32) * 5 / 9, 1)


def _mph_to_kph(mph: float | int | None) -> float | None:
    if mph is None:
        return None
    return round(float(mph) * 1.609344, 1)


def extract_weather_fields(weather: dict | None) -> dict:
    """Pull the fields we surface to the coach out of Garmin's weather payload.

    Returns a dict with temp_c / apparent_temp_c / humidity_pct / wind_kph / label,
    each None when the field isn't present.

    Garmin's /activity/{id}/weather endpoint returns temperatures in Fahrenheit and
    wind in mph regardless of the account's measurementSystem preference, and the
    payload carries no unit field. We always convert F→C and mph→kph here; humidity
    is unit-agnostic.
    """
    if not weather or not isinstance(weather, dict):
        return {"temp_c": None, "apparent_temp_c": None, "humidity_pct": None,
                "wind_kph": None, "label": None}
    label = None
    wt = weather.get("weatherTypeDTO")
    if isinstance(wt, dict):
        label = wt.get("desc") or wt.get("weatherTypeDesc")
    label = label or weather.get("weatherTypeDesc")
    return {
        "temp_c": _f_to_c(weather.get("temp")),
        "apparent_temp_c": _f_to_c(weather.get("apparentTemp")),
        "humidity_pct": weather.get("relativeHumidity"),
        "wind_kph": _mph_to_kph(weather.get("windSpeed")),
        "label": label,
    }


def extract_subjective_fields(detail: dict | None) -> dict:
    """Pull post-run RPE + Feel out of the activity-detail payload.

    These come from the watch's "How did that feel?" prompt and live in
    summaryDTO on the /activity/{id} detail endpoint — NOT the activity-list
    summary the poller iterates. Garmin stores both on a 0–100 scale; RPE is
    rescaled to the 1–10 the coach reports against (logged 3/10 → stored 30 →
    3.0). Feel stays 0–100 (format_feel maps it to Weak/Normal/Strong/etc.).
    """
    summary = (detail or {}).get("summaryDTO") or {}
    raw_rpe = summary.get("directWorkoutRpe")
    feel = summary.get("directWorkoutFeel")
    rpe = (raw_rpe / 10) if raw_rpe is not None else None
    return {"rpe": rpe, "feel": feel}


def extract_running_dynamics(activity: dict) -> dict:
    """Pull Garmin running-dynamics fields from the activity-list payload.

    Native Garmin units, stored as-is: ground contact time in ms, step length
    in cm (Garmin labels it "stride" but it's per-step, matching the steps/min
    cadence), vertical oscillation in cm. Absent on devices without a compatible
    HRM/pod, so each may be None.
    """
    return {
        "ground_contact_ms": activity.get("avgGroundContactTime"),
        "stride_length_cm": activity.get("avgStrideLength"),
        "vertical_oscillation_cm": activity.get("avgVerticalOscillation"),
    }


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

        distance_km = (activity.get("distance") or 0) / 1000
        if distance_km < 1.0:
            log.info(
                "Skipping short activity %s (%.2fkm) — likely a watch test or warm-up",
                activity_id, distance_km,
            )
            continue

        log.info("New activity found: %s (ID: %s)", activity.get("activityName"), activity_id)

        # Fetch splits + HR zones + weather
        splits = garmin.get_activity_splits(activity_id)
        hr_zones = garmin.get_activity_hr_zones(activity_id)
        # Weather may be absent (indoor / treadmill); never block the run on it.
        try:
            weather = garmin.get_activity_weather(activity_id)
        except Exception as e:
            log.warning("Weather fetch failed for %s: %s", activity_id, e)
            weather = None
        weather_fields = extract_weather_fields(weather)

        # Extract and store key metrics
        duration_seconds = activity.get("duration") or 0
        avg_speed = activity.get("averageSpeed") or 0
        avg_pace = format_pace(avg_speed)

        avg_cadence = activity.get("averageRunningCadenceInStepsPerMinute")
        elevation_gain = activity.get("elevationGain")
        elevation_loss = activity.get("elevationLoss")
        anaerobic_te = activity.get("anaerobicTrainingEffect")
        # Garmin reports per-activity training load in one of a few field names
        # depending on the device/firmware; try the common ones, leave NULL if absent.
        training_load = (
            activity.get("activityTrainingLoad")
            or activity.get("trainingLoad")
            or (activity.get("summaryDTO") or {}).get("trainingLoad")
        )

        # Post-run RPE + Feel live in the activity-detail summaryDTO, not the
        # list payload above — fetch the detail and pull them from there.
        try:
            detail = garmin.get_activity_detail(activity_id)
        except Exception as e:
            log.warning("Detail fetch failed for %s: %s", activity_id, e)
            detail = None
        subjective = extract_subjective_fields(detail)
        rpe = subjective["rpe"]
        feel = subjective["feel"]
        dynamics = extract_running_dynamics(activity)

        # Capture the runner's UTC offset at the time of the run, so the coach
        # prompt can later derive "today in the runner's local time" without
        # depending on the Pi's clock or a manually-set RUNNER_TIMEZONE.
        tz_offset_minutes = compute_tz_offset_minutes(
            activity.get("startTimeLocal"),
            activity.get("startTimeGMT"),
        )

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
            training_load=training_load,
            hr_zones_json=json.dumps(hr_zones) if hr_zones else None,
            temp_c=weather_fields["temp_c"],
            apparent_temp_c=weather_fields["apparent_temp_c"],
            humidity_pct=weather_fields["humidity_pct"],
            wind_kph=weather_fields["wind_kph"],
            weather_label=weather_fields["label"],
            weather_json=json.dumps(weather) if weather else None,
            rpe=rpe,
            feel=feel,
            tz_offset_minutes=tz_offset_minutes,
            ground_contact_ms=dynamics["ground_contact_ms"],
            stride_length_cm=dynamics["stride_length_cm"],
            vertical_oscillation_cm=dynamics["vertical_oscillation_cm"],
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
            "hr_zones_json": json.dumps(hr_zones) if hr_zones else None,
            "training_load": training_load,
            "temp_c": weather_fields["temp_c"],
            "apparent_temp_c": weather_fields["apparent_temp_c"],
            "humidity_pct": weather_fields["humidity_pct"],
            "wind_kph": weather_fields["wind_kph"],
            "weather_label": weather_fields["label"],
            "rpe": rpe,
            "feel": feel,
            "ground_contact_ms": dynamics["ground_contact_ms"],
            "stride_length_cm": dynamics["stride_length_cm"],
            "vertical_oscillation_cm": dynamics["vertical_oscillation_cm"],
        }

        try:
            coaching = coach.analyze_run(activity_for_coach)
            db.mark_processed(activity_id, coaching)
            send_coaching_message(coaching)
            log.info("Coaching sent for activity %s", activity_id)
            new_count += 1
        except Exception as e:
            log.exception("Failed to analyze/send activity %s", activity_id)
            send_error_alert(f"Analysis send failed for activity {activity_id}: {e!r}")

        # Off-Pi backup after each new activity. Best-effort — never block the
        # poll path on a backup failure.
        try:
            backup_path = run_backup()
            send_backup_to_telegram(
                backup_path,
                caption=f"DB backup after activity {activity_id}",
            )
            log.info("Backup sent to Telegram: %s", backup_path.name)
        except Exception as e:
            log.warning("Post-run backup failed: %s", e)
            send_error_alert(f"Failed to analyze activity {activity_id}: {e}")

    if new_count == 0:
        log.info("No new activities found.")
    else:
        log.info("Processed %d new activities.", new_count)

    # Fetch and store training status snapshot + lactate threshold
    try:
        ts = garmin.get_training_status()
        lt = garmin.get_lactate_threshold()
        lt_pace = None
        lt_hr = None
        if lt:
            # lactate threshold response shape varies; try common keys
            lt_speed = lt.get("calendarDate") and (
                lt.get("lactateThresholdSpeed")
                or lt.get("lactate_threshold_speed_meters_per_second")
            )
            if lt_speed:
                # m/s -> min/km string handled later; here keep as min/km float
                secs_per_km = 1000 / float(lt_speed)
                lt_pace = round(secs_per_km / 60, 2)  # decimal minutes/km
            lt_hr = lt.get("lactateThresholdHeartRate") or lt.get("heartRate")

        if ts:
            db.save_training_status(
                snapshot_date=date.today().isoformat(),
                training_load_7d=ts.get("weeklyTrainingLoad"),
                recovery_time_hours=ts.get("recoveryTimeInHours"),
                vo2max=ts.get("vo2MaxValue"),
                training_status_label=ts.get("trainingStatusLabel"),
                raw_json=json.dumps(ts),
                lt_pace_min_km=lt_pace,
                lt_hr=lt_hr,
            )
            log.info("Training status snapshot saved (LT pace=%s, LT HR=%s)", lt_pace, lt_hr)
    except Exception as e:
        log.warning("Failed to save training status: %s", e)

    # Fetch and store the most recent complete night's wellness (sleep, HRV, RHR).
    # Garmin dates a sleep record by the wake-up (calendar) date, so TODAY's record
    # is the night just completed — that's the freshest data. Only if today hasn't
    # synced yet (e.g. an overnight poll before the watch uploads) do we fall back to
    # yesterday. Fetching today-1 unconditionally (the old behaviour) always lagged a
    # night behind and the staleness check then masked it as "fresh".
    try:
        wellness_date = date.today()
        sleep_data = garmin.get_sleep(wellness_date)
        synced = bool(((sleep_data or {}).get("dailySleepDTO") or {}).get("sleepTimeSeconds"))
        if not synced:
            wellness_date = date.today() - timedelta(days=1)
            sleep_data = garmin.get_sleep(wellness_date)
        hrv_data = garmin.get_hrv(wellness_date)
        rhr_data = garmin.get_rhr(wellness_date)

        # Sleep payload: dailySleepDTO contains sleepTimeSeconds, overall sleepScores
        sleep_seconds = None
        sleep_score = None
        if sleep_data:
            dto = sleep_data.get("dailySleepDTO") or {}
            sleep_seconds = dto.get("sleepTimeSeconds")
            scores = dto.get("sleepScores") or {}
            sleep_score = (scores.get("overall") or {}).get("value")

        # HRV payload: hrvSummary contains lastNightAvg, weeklyAvg, status
        hrv_last_night = None
        hrv_7d_avg = None
        hrv_status = None
        if hrv_data:
            summary = hrv_data.get("hrvSummary") or {}
            hrv_last_night = summary.get("lastNightAvg")
            hrv_7d_avg = summary.get("weeklyAvg")
            hrv_status = summary.get("status")

        # RHR payload shape varies; try a couple of keys
        rhr = None
        if rhr_data:
            metrics = rhr_data.get("allMetrics") or {}
            rhr_list = metrics.get("metricsMap", {}).get("WELLNESS_RESTING_HEART_RATE")
            if isinstance(rhr_list, list) and rhr_list:
                rhr = rhr_list[0].get("value")
            else:
                rhr = rhr_data.get("restingHeartRate")

        if any(v is not None for v in [sleep_seconds, sleep_score, hrv_last_night, rhr]):
            db.save_daily_wellness(
                target_date=wellness_date.isoformat(),
                sleep_seconds=sleep_seconds,
                sleep_score=sleep_score,
                hrv_last_night=hrv_last_night,
                hrv_7d_avg=hrv_7d_avg,
                hrv_status=hrv_status,
                rhr=rhr,
                raw_json=json.dumps({
                    "sleep": sleep_data,
                    "hrv": hrv_data,
                    "rhr": rhr_data,
                }),
            )
            log.info(
                "Daily wellness saved for %s (sleep=%ss, score=%s, HRV=%s, RHR=%s)",
                wellness_date, sleep_seconds, sleep_score, hrv_last_night, rhr,
            )
        else:
            log.info("No wellness data available for %s", wellness_date)
    except Exception as e:
        log.warning("Failed to save daily wellness: %s", e)

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

    # Heartbeat: mark this poll as complete so the daily alert can detect a
    # stalled poller (no successful completion in >6h means 3 polls missed).
    db.mark_poll_completed()
    db.close()


if __name__ == "__main__":
    poll()
