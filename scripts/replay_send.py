#!/usr/bin/env python3
"""One-off diagnostic: re-send the saved coaching response for activity 22686607591.

Reproduces today's silent send failure with a full traceback. If the send
succeeds, the originally-missing analysis is delivered as a side effect.

Usage:
    python -m scripts.replay_send                # default: activity 22686607591
    python -m scripts.replay_send <activity_id>  # any other activity
"""

import logging
import sqlite3
import sys
import traceback

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

from src.config import DB_PATH
from src.telegram_bot import send_coaching_message


DEFAULT_ACTIVITY_ID = 22686607591


def main() -> int:
    activity_id = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ACTIVITY_ID

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT activity_id, start_time, coaching_response FROM activities WHERE activity_id = ?",
        (activity_id,),
    ).fetchone()

    if row is None:
        print(f"No activity with id {activity_id} in DB.")
        return 1
    if not row["coaching_response"]:
        print(f"Activity {activity_id} has no saved coaching_response.")
        return 1

    text = row["coaching_response"]
    print(f"Replaying activity {activity_id} (start_time={row['start_time']}); "
          f"text length={len(text)} chars")
    print(f"First 80 chars: {text[:80]!r}")
    print(f"Last 80 chars:  {text[-80:]!r}")
    print("---")

    try:
        send_coaching_message(text)
        print("OK: send_coaching_message returned without error.")
        return 0
    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e!r}")
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
