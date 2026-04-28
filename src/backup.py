"""Daily SQLite backup with rotation.

Uses SQLite's native `.backup()` API for atomic snapshots — safe to run while
the bot is actively writing to the DB. Compresses output with gzip and rotates
the backup directory to keep only the most recent RETENTION_DAYS files.

Designed to be invoked two ways:
1. Daily systemd timer (`python -m src.backup`) — keeps a rolling local archive.
2. Programmatically from poller.py / telegram_bot.py — `run_backup()` returns
   the path of the freshly written .db.gz so it can be sent off-Pi via Telegram.
"""

import gzip
import logging
import shutil
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from .config import DB_PATH

log = logging.getLogger(__name__)

BACKUP_DIR = DB_PATH.parent / "backups"
RETENTION_DAYS = 14


def run_backup() -> Path:
    """Snapshot the live DB to data/backups/coach-YYYYMMDD.db.gz, rotate old files,
    and return the path of the new compressed backup."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    raw_target = BACKUP_DIR / f"coach-{today}.db"
    gz_target = BACKUP_DIR / f"coach-{today}.db.gz"

    src = sqlite3.connect(str(DB_PATH))
    dst = sqlite3.connect(str(raw_target))
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()

    with raw_target.open("rb") as r, gzip.open(gz_target, "wb", compresslevel=6) as w:
        shutil.copyfileobj(r, w)
    raw_target.unlink()

    log.info("Backup written: %s (%d bytes)", gz_target, gz_target.stat().st_size)

    _rotate_old_backups()
    return gz_target


def _rotate_old_backups() -> None:
    cutoff = date.today() - timedelta(days=RETENTION_DAYS)
    for f in BACKUP_DIR.glob("coach-*.db.gz"):
        try:
            stem = f.name.removeprefix("coach-").removesuffix(".db.gz")
            file_date = date.fromisoformat(stem)
        except ValueError:
            continue
        if file_date < cutoff:
            f.unlink()
            log.info("Rotated out old backup: %s", f.name)


def latest_backup() -> Path | None:
    """Return the newest backup file in the directory, or None if none exist."""
    if not BACKUP_DIR.exists():
        return None
    files = sorted(BACKUP_DIR.glob("coach-*.db.gz"))
    return files[-1] if files else None


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    run_backup()
