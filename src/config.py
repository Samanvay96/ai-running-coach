import logging
import os
import zoneinfo
from pathlib import Path
from datetime import date
from dotenv import load_dotenv

load_dotenv()

# Paths
PROJECT_ROOT = Path(__file__).parent.parent
TRAINING_PLAN_PATH = PROJECT_ROOT / "Lisbon Marathon Sub4 Plan.xlsx"
DB_PATH = PROJECT_ROOT / "data" / "coach.db"

# API keys and credentials
GARMIN_EMAIL = os.environ["GARMIN_EMAIL"]
GARMIN_PASSWORD = os.environ["GARMIN_PASSWORD"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

# Training plan constants
PLAN_START_DATE = date(2026, 3, 2)  # Monday of week 1
RACE_DATE = date(2026, 10, 10)
TARGET_FINISH = "3:57:57"
TARGET_PACE_KM = "5:40"

# Runner physiology — set RUNNER_AGE in .env. Used to derive MAX_HR via 220-age.
# Formula is approximate (±10 bpm typical); good enough for directional Z2 % math.
AGE = int(os.environ.get("RUNNER_AGE", "30"))
MAX_HR = 220 - AGE

# Runner's timezone — fallback only. The coach prefers the UTC offset of
# the latest activity (auto-tracks travel), and only falls back to this env
# var when there's no recent run. Set to an IANA name like "Europe/London"
# or "Australia/Sydney" for a useful fallback; defaults to UTC.
RUNNER_TIMEZONE = os.environ.get("RUNNER_TIMEZONE", "UTC")
try:
    RUNNER_TZ = zoneinfo.ZoneInfo(RUNNER_TIMEZONE)
except zoneinfo.ZoneInfoNotFoundError:
    logging.getLogger(__name__).warning(
        "Unknown RUNNER_TIMEZONE %r — falling back to UTC", RUNNER_TIMEZONE
    )
    RUNNER_TIMEZONE = "UTC"
    RUNNER_TZ = zoneinfo.ZoneInfo("UTC")
