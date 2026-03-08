import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS activities (
    activity_id INTEGER PRIMARY KEY,
    start_time TEXT NOT NULL,
    activity_type TEXT,
    distance_km REAL,
    duration_seconds REAL,
    avg_pace_min_km TEXT,
    avg_hr INTEGER,
    max_hr INTEGER,
    calories INTEGER,
    aerobic_te REAL,
    vo2max REAL,
    raw_json TEXT,
    splits_json TEXT,
    processed INTEGER DEFAULT 0,
    coaching_response TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS garmin_session (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    token_dir TEXT,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


class Database:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def activity_exists(self, activity_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM activities WHERE activity_id = ?", (activity_id,)
        ).fetchone()
        return row is not None

    def save_activity(self, activity_id: int, start_time: str, activity_type: str,
                      distance_km: float, duration_seconds: float, avg_pace: str,
                      avg_hr: int | None, max_hr: int | None, calories: int | None,
                      aerobic_te: float | None, vo2max: float | None,
                      raw_json: str, splits_json: str) -> None:
        self.conn.execute(
            """INSERT OR IGNORE INTO activities
            (activity_id, start_time, activity_type, distance_km, duration_seconds,
             avg_pace_min_km, avg_hr, max_hr, calories, aerobic_te, vo2max,
             raw_json, splits_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (activity_id, start_time, activity_type, distance_km, duration_seconds,
             avg_pace, avg_hr, max_hr, calories, aerobic_te, vo2max,
             raw_json, splits_json)
        )
        self.conn.commit()

    def get_unprocessed_activities(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM activities WHERE processed = 0 ORDER BY start_time"
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_processed(self, activity_id: int, coaching_response: str) -> None:
        self.conn.execute(
            "UPDATE activities SET processed = 1, coaching_response = ? WHERE activity_id = ?",
            (coaching_response, activity_id)
        )
        self.conn.commit()

    def get_recent_activities(self, limit: int = 10) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM activities ORDER BY start_time DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def save_garmin_token_dir(self, token_dir: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO garmin_session (id, token_dir, updated_at) "
            "VALUES (1, ?, CURRENT_TIMESTAMP)",
            (token_dir,)
        )
        self.conn.commit()

    def get_garmin_token_dir(self) -> str | None:
        row = self.conn.execute(
            "SELECT token_dir FROM garmin_session WHERE id = 1"
        ).fetchone()
        return row["token_dir"] if row else None

    def save_conversation(self, role: str, content: str) -> None:
        self.conn.execute(
            "INSERT INTO conversations (role, content) VALUES (?, ?)",
            (role, content)
        )
        self.conn.commit()

    def get_recent_conversations(self, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            "SELECT role, content FROM conversations ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def close(self):
        self.conn.close()
