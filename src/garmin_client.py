import json
import logging
from datetime import date
from pathlib import Path

from garminconnect import Garmin

from .db import Database
from .retry import with_retry as _with_retry

log = logging.getLogger(__name__)

GARMIN_TOKEN_DIR = Path.home() / ".garminconnect"

RUNNING_TYPES = {
    "running", "track_running", "trail_running", "treadmill_running",
}


def _primary_device_entry(device_map) -> dict:
    """Pick one device's data out of a {deviceId: {...}} map.

    Garmin keys these by device ID, so the key is not knowable in advance and
    can change when the runner switches watches. Prefer the device flagged
    primaryTrainingDevice; otherwise take any entry. Returns {} when there's
    nothing usable so callers can keep using .get().
    """
    if not isinstance(device_map, dict):
        return {}
    entries = [v for v in device_map.values() if isinstance(v, dict)]
    if not entries:
        return {}
    for e in entries:
        if e.get("primaryTrainingDevice"):
            return e
    return entries[0]


def parse_training_status(payload) -> dict:
    """Flatten Garmin's nested training-status payload.

    Every field here used to be read off the top level, where none of them
    exist — so the poller wrote a row of NULLs on every poll and the coach's
    training-status context was permanently "Not available".

    recovery_time_hours has no source: it is absent from this payload, from
    get_user_summary and from the activity detail, so it stays None rather than
    being faked.
    """
    if not isinstance(payload, dict):
        return {}
    status = _primary_device_entry(
        (payload.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData")
    )
    acute = status.get("acuteTrainingLoadDTO") or {}
    vo2 = ((payload.get("mostRecentVO2Max") or {}).get("generic") or {})
    return {
        "vo2max": vo2.get("vo2MaxPreciseValue") or vo2.get("vo2MaxValue"),
        # weeklyTrainingLoad is present but null on this account; the acute
        # (7-day rolling) load is the figure the watch actually shows.
        "training_load_7d": status.get("weeklyTrainingLoad") or acute.get("dailyTrainingLoadAcute"),
        "training_status_label": status.get("trainingStatusFeedbackPhrase"),
        "recovery_time_hours": None,
        "snapshot_date": status.get("calendarDate") or vo2.get("calendarDate"),
    }


# A threshold pace outside this range means we misread the units — refuse it
# rather than feed the coach a 53 min/km "threshold".
_LT_MIN_SECS_PER_KM = 150    # 2:30/km — faster than any human threshold pace
_LT_MAX_SECS_PER_KM = 900    # 15:00/km


def parse_lactate_threshold(payload) -> dict:
    """Pull LT pace and HR out of the nested lactate-threshold payload.

    Shape is {"speed_and_heart_rate": {"speed": ..., "heartRate": ..., }, "power": {}}.
    The old code read "lactateThresholdSpeed"/"calendarDate" off the top level,
    where neither exists, so LT never landed.

    UNITS: despite the name, `speed` is seconds per metre, not m/s. Verified by
    comparison — an activity's averageSpeed of 2.624 m/s is a 6:21/km run, so
    this account's 0.3138 would be ~53 min/km if it were m/s. Read as s/m it
    gives 5:14/km, which is coherent against LT HR 171. Anything outside a
    plausible pace range is dropped.
    """
    if not isinstance(payload, dict):
        return {}
    core = payload.get("speed_and_heart_rate")
    if not isinstance(core, dict):
        return {}
    pace_min_km = None
    speed = core.get("speed")
    if isinstance(speed, (int, float)) and speed > 0:
        secs_per_km = float(speed) * 1000
        if _LT_MIN_SECS_PER_KM <= secs_per_km <= _LT_MAX_SECS_PER_KM:
            pace_min_km = round(secs_per_km / 60, 2)
    raw_date = core.get("calendarDate")
    return {
        "lt_pace_min_km": pace_min_km,
        "lt_hr": core.get("heartRate"),
        # Garmin auto-detects LT only during hard sustained efforts, so this can
        # be months old. The coach needs the date to know whether to trust it.
        "lt_date": str(raw_date)[:10] if raw_date else None,
    }


def select_hr_zone_entry(payload) -> dict | None:
    """Pick the authoritative zone config from the heartRateZones payload.

    Garmin returns a list with one entry per sport. RUNNING wins when present;
    DEFAULT is the fallback (they usually agree, but a sport-specific override
    is the one actually applied to runs). Returns None for anything unusable so
    callers fall back rather than persisting a half-parsed row.
    """
    if not isinstance(payload, list):
        return None
    entries = [e for e in payload if isinstance(e, dict)]
    by_sport = {str(e.get("sport") or "").upper(): e for e in entries}
    entry = by_sport.get("RUNNING") or by_sport.get("DEFAULT") or (entries[0] if entries else None)
    if not entry or not entry.get("zone2Floor") or not entry.get("zone3Floor"):
        return None  # Without the Z2/Z3 floors there's no band to derive.
    return entry


class GarminClient:
    def __init__(self, email: str, password: str, db: Database):
        self.email = email
        self.password = password
        self.db = db
        self.api = Garmin(email, password)
        self._login()

    def _login(self):
        token_dir = self.db.get_garmin_token_dir()
        if token_dir and Path(token_dir).exists():
            try:
                self.api.login(token_dir)
                log.info("Resumed Garmin session from saved tokens")
                return
            except Exception:
                log.warning("Saved tokens expired, re-authenticating")

        self.api.login()
        self.api.garth.dump(str(GARMIN_TOKEN_DIR))
        self.db.save_garmin_token_dir(str(GARMIN_TOKEN_DIR))
        log.info("Fresh Garmin login, tokens saved")

    def get_recent_activities(self, limit: int = 10) -> list[dict]:
        activities = self.api.get_activities(0, limit)
        return [
            a for a in activities
            if a.get("activityType", {}).get("typeKey") in RUNNING_TYPES
        ]

    def get_activity_detail(self, activity_id: int) -> dict | None:
        """Full activity detail (the /activity/{id} endpoint).

        Carries fields absent from the activity-list summary — notably
        summaryDTO.directWorkoutRpe / directWorkoutFeel from the watch's
        post-run "How did that feel?" prompt.
        """
        return _with_retry(
            self.api.get_activity, activity_id,
            _label=f"detail for activity {activity_id}",
        )

    def get_activity_splits(self, activity_id: int) -> list[dict]:
        return _with_retry(
            self.api.get_activity_splits, activity_id,
            _label=f"splits for activity {activity_id}",
        ) or []

    def get_activity_hr_zones(self, activity_id: int) -> list[dict]:
        return _with_retry(
            self.api.get_activity_hr_in_timezones, activity_id,
            _label=f"HR zones for activity {activity_id}",
        ) or []

    def get_activity_weather(self, activity_id: int) -> dict | None:
        """Garmin's weather snapshot for the activity (temp, humidity, wind, etc.).

        Returns None for indoor / treadmill runs where Garmin has no weather record.
        """
        return _with_retry(
            self.api.get_activity_weather, activity_id,
            _label=f"weather for activity {activity_id}",
        )

    def get_training_status(self) -> dict | None:
        return _with_retry(
            self.api.get_training_status, date.today().isoformat(),
            _label="training status",
        )

    def get_sleep(self, target_date) -> dict | None:
        return _with_retry(
            self.api.get_sleep_data, target_date.isoformat(),
            _label=f"sleep data for {target_date}",
        )

    def get_hrv(self, target_date) -> dict | None:
        return _with_retry(
            self.api.get_hrv_data, target_date.isoformat(),
            _label=f"HRV data for {target_date}",
        )

    def get_rhr(self, target_date) -> dict | None:
        return _with_retry(
            self.api.get_rhr_day, target_date.isoformat(),
            _label=f"RHR for {target_date}",
        )

    def get_lactate_threshold(self) -> dict | None:
        return _with_retry(self.api.get_lactate_threshold, _label="lactate threshold")

    def get_hr_zones(self) -> list[dict] | None:
        """Heart rate zones as configured on Garmin.

        `garminconnect` has no wrapper for this path, so we go through
        `connectapi` directly. Returns one entry per sport, each carrying
        maxHeartRateUsed / restingHeartRateUsed and the zone floors.

        This matters because Garmin's per-activity zone buckets — which
        `compute_zone_distribution` already prefers — are computed from exactly
        these numbers. Deriving our own band from 220−age instead put the stated
        Z2 range 5 bpm below the buckets we were reporting against.
        """
        return _with_retry(
            self.api.connectapi, "/biometric-service/heartRateZones",
            _label="HR zones",
        )
