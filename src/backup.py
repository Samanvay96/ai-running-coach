"""Daily SQLite and plan.yaml backup with rotation.

Uses SQLite's native `.backup()` API for atomic snapshots — safe to run while
the bot is actively writing to the DB. Compresses output with gzip and rotates
the backup directory to keep only the most recent RETENTION_DAYS files.

Designed to be invoked two ways:
1. Daily systemd timer (`python -m src.backup`) — keeps a rolling local archive.
2. Programmatically from poller.py / telegram_bot.py — `run_backup()` returns
   the path of the freshly written .db.gz so it can be sent off-Pi via Telegram.
"""

import gzip
import hashlib
import logging
import shutil
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from .config import DB_PATH, TRAINING_PLAN_PATH

log = logging.getLogger(__name__)

BACKUP_DIR = DB_PATH.parent / "backups"
RETENTION_DAYS = 14
# Hash of the plan version last uploaded to Telegram. Compared against the live
# plan, not yesterday's local copy, so a failed upload is retried next night.
PLAN_SENT_MARKER = "plan-last-sent.sha256"


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

    _rotate_old_backups("coach-", ".db.gz")
    return gz_target


def backup_plan(plan_path: Path | None = None) -> Path | None:
    """Copy plan.yaml to data/backups/plan-YYYY-MM-DD.yaml and rotate old copies.

    plan.yaml is gitignored, so this is its only history. Returns the copy, or
    None when there is no plan file to back up.
    """
    plan_path = plan_path or TRAINING_PLAN_PATH
    if not plan_path.exists():
        log.warning("No plan file at %s — nothing to back up", plan_path)
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    target = BACKUP_DIR / f"plan-{date.today().isoformat()}.yaml"
    tmp = target.with_suffix(".yaml.tmp")
    tmp.write_bytes(plan_path.read_bytes())
    tmp.replace(target)
    log.info("Plan backup written: %s", target)
    _rotate_old_backups("plan-", ".yaml")
    return target


def plan_needs_upload(copy: Path) -> bool:
    marker = BACKUP_DIR / PLAN_SENT_MARKER
    sent = marker.read_text().strip() if marker.exists() else ""
    return _sha256(copy) != sent


def mark_plan_uploaded(copy: Path) -> None:
    (BACKUP_DIR / PLAN_SENT_MARKER).write_text(_sha256(copy) + "\n")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_daily() -> None:
    """The 02:00 timer: DB snapshot, plan copy, and an off-Pi upload of the plan
    whenever it differs from the last version that reached Telegram."""
    run_backup()
    copy = backup_plan()
    if copy is None or not plan_needs_upload(copy):
        return
    from .telegram_bot import send_backup_to_telegram
    if send_backup_to_telegram(copy, caption=f"Plan changed — backup {copy.name}"):
        mark_plan_uploaded(copy)
        log.info("Plan backup sent to Telegram: %s", copy.name)
    else:
        log.warning("Plan upload failed; will retry at the next daily backup")


def _rotate_old_backups(prefix: str, suffix: str) -> None:
    cutoff = date.today() - timedelta(days=RETENTION_DAYS)
    for f in BACKUP_DIR.glob(f"{prefix}*{suffix}"):
        try:
            file_date = date.fromisoformat(f.name.removeprefix(prefix).removesuffix(suffix))
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
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    run_daily()
