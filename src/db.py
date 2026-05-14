import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .time_utils import compute_tz_offset_minutes

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
    avg_cadence REAL,
    elevation_gain REAL,
    elevation_loss REAL,
    anaerobic_te REAL,
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

CREATE TABLE IF NOT EXISTS training_status (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date TEXT NOT NULL,
    training_load_7d REAL,
    recovery_time_hours INTEGER,
    vo2max REAL,
    training_status_label TEXT,
    raw_json TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS weekly_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_number INTEGER NOT NULL,
    week_start TEXT NOT NULL,
    week_end TEXT NOT NULL,
    sent_at TEXT DEFAULT CURRENT_TIMESTAMP,
    summary_text TEXT
);

CREATE TABLE IF NOT EXISTS daily_wellness (
    date TEXT PRIMARY KEY,
    sleep_seconds INTEGER,
    sleep_score INTEGER,
    hrv_last_night REAL,
    hrv_7d_avg REAL,
    hrv_status TEXT,
    rhr INTEGER,
    raw_json TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS system_health (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_poll_completed_at TEXT
);
"""

MIGRATIONS = [
    "ALTER TABLE activities ADD COLUMN avg_cadence REAL",
    "ALTER TABLE activities ADD COLUMN elevation_gain REAL",
    "ALTER TABLE activities ADD COLUMN elevation_loss REAL",
    "ALTER TABLE activities ADD COLUMN anaerobic_te REAL",
    "ALTER TABLE activities ADD COLUMN training_load REAL",
    "ALTER TABLE activities ADD COLUMN hr_zones_json TEXT",
    "ALTER TABLE training_status ADD COLUMN lt_pace_min_km REAL",
    "ALTER TABLE training_status ADD COLUMN lt_hr INTEGER",
    "ALTER TABLE activities ADD COLUMN temp_c REAL",
    "ALTER TABLE activities ADD COLUMN apparent_temp_c REAL",
    "ALTER TABLE activities ADD COLUMN humidity_pct REAL",
    "ALTER TABLE activities ADD COLUMN wind_kph REAL",
    "ALTER TABLE activities ADD COLUMN weather_label TEXT",
    "ALTER TABLE activities ADD COLUMN weather_json TEXT",
    "ALTER TABLE activities ADD COLUMN rpe REAL",
    "ALTER TABLE activities ADD COLUMN feel INTEGER",
    "ALTER TABLE activities ADD COLUMN tz_offset_minutes INTEGER",
]


class Database:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._run_migrations()
        # Idempotent — only does work on rows where the column is still NULL.
        self.backfill_tz_offsets()

    def _run_migrations(self):
        for sql in MIGRATIONS:
            try:
                self.conn.execute(sql)
            except sqlite3.OperationalError:
                pass  # Column already exists

    def activity_exists(self, activity_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM activities WHERE activity_id = ?", (activity_id,)
        ).fetchone()
        return row is not None

    def save_activity(self, activity_id: int, start_time: str, activity_type: str,
                      distance_km: float, duration_seconds: float, avg_pace: str,
                      avg_hr: int | None, max_hr: int | None, calories: int | None,
                      aerobic_te: float | None, vo2max: float | None,
                      raw_json: str, splits_json: str,
                      avg_cadence: float | None = None,
                      elevation_gain: float | None = None,
                      elevation_loss: float | None = None,
                      anaerobic_te: float | None = None,
                      training_load: float | None = None,
                      hr_zones_json: str | None = None,
                      temp_c: float | None = None,
                      apparent_temp_c: float | None = None,
                      humidity_pct: float | None = None,
                      wind_kph: float | None = None,
                      weather_label: str | None = None,
                      weather_json: str | None = None,
                      rpe: float | None = None,
                      feel: int | None = None,
                      tz_offset_minutes: int | None = None) -> None:
        self.conn.execute(
            """INSERT OR IGNORE INTO activities
            (activity_id, start_time, activity_type, distance_km, duration_seconds,
             avg_pace_min_km, avg_hr, max_hr, calories, aerobic_te, vo2max,
             avg_cadence, elevation_gain, elevation_loss, anaerobic_te,
             raw_json, splits_json, training_load, hr_zones_json,
             temp_c, apparent_temp_c, humidity_pct, wind_kph, weather_label, weather_json,
             rpe, feel, tz_offset_minutes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (activity_id, start_time, activity_type, distance_km, duration_seconds,
             avg_pace, avg_hr, max_hr, calories, aerobic_te, vo2max,
             avg_cadence, elevation_gain, elevation_loss, anaerobic_te,
             raw_json, splits_json, training_load, hr_zones_json,
             temp_c, apparent_temp_c, humidity_pct, wind_kph, weather_label, weather_json,
             rpe, feel, tz_offset_minutes)
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

    def get_activities_for_range(self, start_date: str, end_date: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM activities WHERE start_time >= ? AND start_time < ? ORDER BY start_time",
            (start_date, end_date)
        ).fetchall()
        return [dict(r) for r in rows]

    def save_training_status(self, snapshot_date: str, training_load_7d: float | None,
                             recovery_time_hours: int | None, vo2max: float | None,
                             training_status_label: str | None, raw_json: str,
                             lt_pace_min_km: float | None = None,
                             lt_hr: int | None = None) -> None:
        self.conn.execute(
            """INSERT INTO training_status
            (snapshot_date, training_load_7d, recovery_time_hours, vo2max, training_status_label, raw_json,
             lt_pace_min_km, lt_hr)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (snapshot_date, training_load_7d, recovery_time_hours, vo2max, training_status_label, raw_json,
             lt_pace_min_km, lt_hr)
        )
        self.conn.commit()

    def get_latest_training_status(self) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM training_status ORDER BY snapshot_date DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def weekly_summary_sent(self, week_start: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM weekly_summaries WHERE week_start = ?", (week_start,)
        ).fetchone()
        return row is not None

    def save_daily_wellness(self, target_date: str, sleep_seconds: int | None,
                            sleep_score: int | None, hrv_last_night: float | None,
                            hrv_7d_avg: float | None, hrv_status: str | None,
                            rhr: int | None, raw_json: str) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO daily_wellness
            (date, sleep_seconds, sleep_score, hrv_last_night, hrv_7d_avg, hrv_status, rhr, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (target_date, sleep_seconds, sleep_score, hrv_last_night, hrv_7d_avg, hrv_status, rhr, raw_json)
        )
        self.conn.commit()

    def get_latest_wellness(self) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM daily_wellness ORDER BY date DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def get_wellness_for_range(self, start_date: str, end_date: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM daily_wellness WHERE date >= ? AND date <= ? ORDER BY date",
            (start_date, end_date)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_training_load_sum(self, start_date: str, end_date: str) -> float:
        """Sum activities.training_load between two ISO dates (inclusive on both ends).
        Used for acute:chronic load ratio."""
        row = self.conn.execute(
            """SELECT COALESCE(SUM(training_load), 0) AS total
               FROM activities
               WHERE training_load IS NOT NULL
                 AND start_time >= ? AND start_time < ?""",
            (start_date, end_date)
        ).fetchone()
        return float(row["total"] or 0)

    def get_distance_sum(self, start_date: str, end_date: str) -> float:
        row = self.conn.execute(
            """SELECT COALESCE(SUM(distance_km), 0) AS total
               FROM activities
               WHERE start_time >= ? AND start_time < ?""",
            (start_date, end_date)
        ).fetchone()
        return float(row["total"] or 0)

    def get_latest_tz_offset_minutes(self, within_days: int = 14) -> int | None:
        """Most recent activity's UTC offset, only if the run was within `within_days`.

        Filtering by recency means an old offset from a past trip can't keep
        overriding the runner's actual current location if they don't run for
        a while. Falls to None on no match — caller should fall back to env.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=within_days)).strftime("%Y-%m-%d")
        row = self.conn.execute(
            """SELECT tz_offset_minutes FROM activities
               WHERE tz_offset_minutes IS NOT NULL AND start_time >= ?
               ORDER BY start_time DESC LIMIT 1""",
            (cutoff,),
        ).fetchone()
        return int(row["tz_offset_minutes"]) if row else None

    def backfill_tz_offsets(self) -> int:
        """One-time pass: derive tz_offset_minutes from raw_json for rows where it's NULL.

        Idempotent — after the first call populates everything, subsequent calls do
        no writes (the WHERE clause filters to NULL rows). Runs on every Database
        open so existing installs auto-populate without a manual script.
        Returns the number of rows updated.
        """
        rows = self.conn.execute(
            """SELECT activity_id, raw_json FROM activities
               WHERE tz_offset_minutes IS NULL AND raw_json IS NOT NULL"""
        ).fetchall()
        n = 0
        for row in rows:
            try:
                raw = json.loads(row["raw_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            offset = compute_tz_offset_minutes(
                raw.get("startTimeLocal"),
                raw.get("startTimeGMT"),
            )
            if offset is None:
                continue
            self.conn.execute(
                "UPDATE activities SET tz_offset_minutes = ? WHERE activity_id = ?",
                (offset, row["activity_id"]),
            )
            n += 1
        if n:
            self.conn.commit()
        return n

    def save_weekly_summary(self, week_number: int, week_start: str, week_end: str,
                            summary_text: str) -> None:
        self.conn.execute(
            """INSERT INTO weekly_summaries (week_number, week_start, week_end, summary_text)
            VALUES (?, ?, ?, ?)""",
            (week_number, week_start, week_end, summary_text)
        )
        self.conn.commit()

    def mark_poll_completed(self) -> None:
        """Record a successful poll completion. The daily alert uses the most
        recent value to detect a stalled poller (no completion in >6h)."""
        self.conn.execute(
            """INSERT INTO system_health (id, last_poll_completed_at)
               VALUES (1, CURRENT_TIMESTAMP)
               ON CONFLICT(id) DO UPDATE SET last_poll_completed_at = CURRENT_TIMESTAMP"""
        )
        self.conn.commit()

    def get_last_poll_completed_at(self) -> datetime | None:
        """Most recent successful poll completion (UTC). None if poller has never run."""
        row = self.conn.execute(
            "SELECT last_poll_completed_at FROM system_health WHERE id = 1"
        ).fetchone()
        if not row or not row["last_poll_completed_at"]:
            return None
        # SQLite CURRENT_TIMESTAMP returns naive UTC like "2026-05-14 10:23:45"
        try:
            return datetime.fromisoformat(row["last_poll_completed_at"]).replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    def close(self):
        self.conn.close()
