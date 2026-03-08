import json
import logging
from pathlib import Path

from garminconnect import Garmin

from .db import Database

log = logging.getLogger(__name__)

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
        try:
            return self.api.get_activity_splits(activity_id)
        except Exception:
            log.warning("Could not fetch splits for activity %s", activity_id)
            return []

    def get_activity_hr_zones(self, activity_id: int) -> list[dict]:
        try:
            return self.api.get_activity_hr_in_timezones(activity_id)
        except Exception:
            log.warning("Could not fetch HR zones for activity %s", activity_id)
            return []

    def get_training_status(self) -> dict | None:
        try:
            return self.api.get_training_status(0)
        except Exception:
            log.warning("Could not fetch training status")
            return None
