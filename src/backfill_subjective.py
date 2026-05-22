"""One-shot backfill for post-run RPE/Feel on existing activities.

Until the May 2026 ingest fix, the poller read RPE/Feel off the activity-LIST
payload, where those keys don't exist — so they were stored as NULL even when
the runner logged them on the watch. The data still lives in Garmin's activity-
DETAIL endpoint (summaryDTO.directWorkoutRpe/Feel). This walks every activity
still missing both fields, refetches the detail, and fills what's there.

Reuses the live ingest path (GarminClient.get_activity_detail +
poller.extract_subjective_fields), so the RPE 0-100 -> 1-10 rescale stays in one
place. Idempotent — only touches NULL rows, safe to re-run.

Usage:
    python -m src.backfill_subjective         # all activities missing RPE/Feel
    python -m src.backfill_subjective 10      # cap at 10 rows (testing)
"""

import logging
import sys
import time

from .config import GARMIN_EMAIL, GARMIN_PASSWORD, DB_PATH
from .db import Database
from .garmin_client import GarminClient
from .poller import extract_subjective_fields

log = logging.getLogger(__name__)


def backfill(limit: int | None = None) -> tuple[int, int]:
    """Fill RPE/Feel for activities missing both. Returns (filled, skipped)."""
    db = Database(DB_PATH)
    try:
        garmin = GarminClient(GARMIN_EMAIL, GARMIN_PASSWORD, db)
    except Exception as e:
        log.error("Garmin login failed: %s", e)
        db.close()
        return (0, 0)

    rows = db.conn.execute(
        "SELECT activity_id, start_time FROM activities "
        "WHERE rpe IS NULL AND feel IS NULL ORDER BY start_time"
    ).fetchall()
    if limit is not None:
        rows = rows[:limit]
    log.info("Found %d activities missing RPE/Feel", len(rows))

    filled = 0
    skipped = 0
    for row in rows:
        activity_id = row["activity_id"]
        try:
            detail = garmin.get_activity_detail(activity_id)
            subjective = extract_subjective_fields(detail)
        except Exception as e:
            log.warning("Detail fetch failed for %s: %s", activity_id, e)
            subjective = {"rpe": None, "feel": None}

        if subjective["rpe"] is not None or subjective["feel"] is not None:
            db.update_activity_subjective(activity_id, subjective["rpe"], subjective["feel"])
            filled += 1
            log.info(
                "%s (%s): rpe=%s feel=%s",
                activity_id, row["start_time"][:10], subjective["rpe"], subjective["feel"],
            )
        else:
            skipped += 1
            log.info("%s (%s): no RPE/Feel logged", activity_id, row["start_time"][:10])

        time.sleep(0.3)  # gentle pacing on Garmin Connect

    db.close()
    log.info("Done. Filled %d, skipped %d (no data logged).", filled, skipped)
    return (filled, skipped)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    cap = int(sys.argv[1]) if len(sys.argv) > 1 else None
    backfill(limit=cap)
