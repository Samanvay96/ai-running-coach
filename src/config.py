import logging
import os
import zoneinfo
from pathlib import Path
from datetime import date, datetime
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

# Runner's current timezone — used so "today" in the coach prompt matches what
# the runner sees on their watch, not the Pi's clock. Set RUNNER_TIMEZONE in
# .env to an IANA name like "Europe/London", "Australia/Sydney", etc. Update
# when travelling.
RUNNER_TIMEZONE = os.environ.get("RUNNER_TIMEZONE", "UTC")
try:
    RUNNER_TZ = zoneinfo.ZoneInfo(RUNNER_TIMEZONE)
except zoneinfo.ZoneInfoNotFoundError:
    logging.getLogger(__name__).warning(
        "Unknown RUNNER_TIMEZONE %r — falling back to UTC", RUNNER_TIMEZONE
    )
    RUNNER_TIMEZONE = "UTC"
    RUNNER_TZ = zoneinfo.ZoneInfo("UTC")


def today_local() -> date:
    """Today as the runner sees it on their watch (runner-timezone aware)."""
    return datetime.now(RUNNER_TZ).date()
