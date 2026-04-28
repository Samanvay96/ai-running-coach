import json
import logging
import time
from pathlib import Path

from garminconnect import Garmin

from .db import Database

log = logging.getLogger(__name__)


def _with_retry(fn, *args, _label: str = "Garmin call", _attempts: int = 3, _backoff: float = 1.0, **kwargs):
    """Run fn with retries + exponential backoff. Returns the call's result, or None on final failure.

    Mirrors the existing fail-soft pattern in this module — log warnings, never raise.
    """
    for attempt in range(_attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if attempt < _attempts - 1:
                wait = _backoff * (2 ** attempt)
                log.warning("%s attempt %d/%d failed: %s (retry in %.1fs)", _label, attempt + 1, _attempts, e, wait)
                time.sleep(wait)
            else:
                log.warning("%s failed after %d attempts: %s", _label, _attempts, e)
    return None

GARMIN_TOKEN_DIR = Path.home() / ".garminconnect"

RUNNING_TYPES = {
    "running", "track_running", "trail_running", "treadmill_running",
}


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

    def get_training_status(self) -> dict | None:
        return _with_retry(self.api.get_training_status, 0, _label="training status")

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
