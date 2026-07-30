import logging
import os
import zoneinfo
from pathlib import Path
from datetime import date
from dotenv import load_dotenv

load_dotenv()

# Paths
PROJECT_ROOT = Path(__file__).parent.parent
TRAINING_PLAN_PATH = PROJECT_ROOT / "Lisbon_Marathon_Finish_Plan_v7.xlsx"
DB_PATH = PROJECT_ROOT / "data" / "coach.db"

# API keys and credentials
GARMIN_EMAIL = os.environ["GARMIN_EMAIL"]
GARMIN_PASSWORD = os.environ["GARMIN_PASSWORD"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

# Training plan constants
PLAN_START_DATE = date(2026, 3, 2)  # Monday of week 1
RACE_DATE = date(2026, 10, 10)      # a Saturday — v5 wrongly called it a Sunday
# Goal finish time and target pace are NOT constants here: they live in the
# xlsx and are read via TrainingPlan.target_finish / .target_pace. Duplicating
# them is what left the coach prompt chasing sub-4:00 (3:57:57 @ 5:40/km) for
# weeks after v6 retired that goal.

# Runner physiology — set RUNNER_AGE in .env. Used to derive MAX_HR via 220-age.
#
# LAST-RESORT FALLBACK ONLY. The Z2 band comes from Garmin's configured zones
# (see coach.resolve_z2_bounds); this formula is used only when that snapshot is
# missing. It is not merely imprecise — for this runner 220-age gives 190 where
# Garmin has 199, which put the Z2 ceiling ~5 bpm low and made correctly-easy
# runs read as too hard.
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
