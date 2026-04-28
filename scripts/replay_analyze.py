#!/usr/bin/env python3
"""One-off diagnostic: re-run coach.analyze_run for activity 22686607591.

Reproduces the silent failure from Apr 28 10:00 with full visibility into
response.content and stop_reason. Prints the raw API response shape before
the fragile `next(...)` line at coach.py:433 runs.
"""

import json
import logging
import sqlite3
import sys
import traceback

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

import anthropic

from src.config import (
    DB_PATH,
    ANTHROPIC_API_KEY,
    TRAINING_PLAN_PATH,
)
from src.db import Database
from src.training_plan import TrainingPlan
from src.coach import Coach, MODEL


DEFAULT_ACTIVITY_ID = 22686607591


def main() -> int:
    pos_args = [a for a in sys.argv[1:] if not a.startswith("--")]
    activity_id = int(pos_args[0]) if pos_args else DEFAULT_ACTIVITY_ID

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM activities WHERE activity_id = ?", (activity_id,)).fetchone()
    if row is None:
        print(f"No activity {activity_id} in DB.")
        return 1

    raw = json.loads(row["raw_json"])
    activity_for_coach = {
        "start_time": row["start_time"],
        "distance_km": row["distance_km"],
        "duration_seconds": row["duration_seconds"],
        "avg_pace_min_km": row["avg_pace_min_km"],
        "avg_hr": row["avg_hr"],
        "max_hr": row["max_hr"],
        "calories": row["calories"],
        "aerobic_te": row["aerobic_te"],
        "anaerobic_te": row["anaerobic_te"],
        "avg_cadence": row["avg_cadence"],
        "elevation_gain": row["elevation_gain"],
        "elevation_loss": row["elevation_loss"],
        "training_effect_label": raw.get("trainingEffectLabel"),
        "splits_json": row["splits_json"],
        "hr_zones_json": row["hr_zones_json"],
        "training_load": row["training_load"],
    }

    db = Database(DB_PATH)
    plan = TrainingPlan(str(TRAINING_PLAN_PATH))
    coach = Coach(ANTHROPIC_API_KEY, plan, db)

    # Build the same user_prompt analyze_run would build, but call the API
    # directly so we can inspect the raw response shape before the next() call.
    print(f"Replaying analyze_run for activity {activity_id} ({row['start_time']})")
    print("Calling API directly to inspect raw response...")

    # Reuse Coach internals to build the prompt by monkey-instrumenting the API call
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    original_create = client.messages.create
    captured = {}

    def wrapped(**kwargs):
        captured["request"] = kwargs
        resp = original_create(**kwargs)
        captured["response"] = resp
        return resp

    client.messages.create = wrapped
    coach.client = client

    try:
        result = coach.analyze_run(activity_for_coach)
        print("---")
        print("OK: analyze_run returned a string of length", len(result))
        print(f"First 200 chars: {result[:200]!r}")
        if "--deliver" in sys.argv:
            from src.telegram_bot import send_coaching_message
            db.mark_processed(activity_id, result)
            send_coaching_message(result)
            print("Saved to DB and sent to Telegram.")
    except Exception as e:
        print("---")
        print(f"FAILED inside analyze_run: {type(e).__name__}: {e!r}")
        traceback.print_exc()
    finally:
        resp = captured.get("response")
        if resp is not None:
            print("---")
            print(f"Response stop_reason: {getattr(resp, 'stop_reason', '?')}")
            print(f"Response usage: {getattr(resp, 'usage', '?')}")
            content = getattr(resp, "content", [])
            print(f"Content blocks: {len(content)}")
            for i, b in enumerate(content):
                btype = getattr(b, "type", "?")
                preview = ""
                if btype == "text":
                    preview = repr(b.text[:120])
                elif btype == "thinking":
                    preview = repr(getattr(b, "thinking", "")[:120])
                print(f"  [{i}] type={btype}  preview={preview}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
