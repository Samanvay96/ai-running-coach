"""Daily proactive overload checks.

Runs once per day (systemd timer) and sends a single combined Telegram nudge if
any of these signals fire:

  - ACR (acute:chronic load ratio) >= 1.5
  - RHR creep: last 3 nights avg >= prior baseline + 5 bpm
  - HRV suppression: 3+ nights flagged or sustained drop vs weekly avg
  - Sleep debt: 3-night avg < 6.5h

Per-alert cooldown of 3 days (tracked in alert_history table) keeps multi-day
overreach from re-firing every morning.
"""

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import mean

from .config import DB_PATH
from .db import Database

log = logging.getLogger(__name__)

COOLDOWN_DAYS = 3


@dataclass
class Alert:
    kind: str
    severity: str
    message: str


def check_acr(db: Database) -> Alert | None:
    from .coach import compute_acr  # imported lazily to avoid pulling Anthropic at module load
    acr = compute_acr(db, date.today())
    if not acr or acr.get("ratio") is None:
        return None
    r = acr["ratio"]
    if r >= 1.5:
        return Alert(
            kind="acr_high",
            severity="high",
            message=(
                f"⚠️ Acute:Chronic load ratio is {r:.2f} "
                f"(acute 7d={acr['acute_7d']}, chronic 28d={acr['chronic_28d']}; "
                f"sweet spot 0.8–1.3, danger >1.5). "
                f"Injury risk is elevated — consider an easier week."
            ),
        )
    return None


def check_rhr(db: Database) -> Alert | None:
    today = date.today()
    rows = db.get_wellness_for_range(
        (today - timedelta(days=14)).isoformat(),
        today.isoformat(),
    )
    rhrs = [r["rhr"] for r in rows if r.get("rhr") is not None]
    if len(rhrs) < 7:
        return None
    recent = mean(rhrs[-3:])
    baseline_pool = rhrs[-14:-3] if len(rhrs) >= 14 else rhrs[:-3]
    if not baseline_pool:
        return None
    baseline = mean(baseline_pool)
    delta = recent - baseline
    if delta >= 5:
        return Alert(
            kind="rhr_creep",
            severity="high",
            message=(
                f"💗 RHR has climbed {delta:.0f} bpm above baseline "
                f"(last 3 nights avg {recent:.0f}, prior {baseline:.0f}). "
                f"Possible overreach or illness brewing — extra rest is wise."
            ),
        )
    return None


def check_hrv(db: Database) -> Alert | None:
    today = date.today()
    rows = db.get_wellness_for_range(
        (today - timedelta(days=7)).isoformat(),
        today.isoformat(),
    )
    rows_with_hrv = [r for r in rows if r.get("hrv_last_night") is not None]
    if len(rows_with_hrv) < 4:
        return None

    last3 = rows_with_hrv[-3:]
    statuses = [r.get("hrv_status") for r in last3]
    weekly_avg = rows_with_hrv[-1].get("hrv_7d_avg")

    # Strongest signal: Garmin itself flags 3+ nights as UNBALANCED.
    if all(s == "UNBALANCED" for s in statuses):
        return Alert(
            kind="hrv_drop",
            severity="high",
            message=(
                "❤️‍🩹 HRV has been flagged UNBALANCED for 3+ consecutive nights. "
                "Body's autonomic-recovery signal is suppressed — strongly consider an extra rest day."
            ),
        )

    # Fallback: ≥15% drop vs the 7-day avg across all of last 3 nights.
    if weekly_avg and all(r["hrv_last_night"] < weekly_avg * 0.85 for r in last3):
        recent_avg = mean(r["hrv_last_night"] for r in last3)
        return Alert(
            kind="hrv_drop",
            severity="moderate",
            message=(
                f"❤️‍🩹 HRV last 3 nights avg {recent_avg:.0f}ms, "
                f"≥15% below your 7-day avg of {weekly_avg:.0f}ms. "
                f"Recovery is trending down — ease off."
            ),
        )
    return None


def check_sleep(db: Database) -> Alert | None:
    today = date.today()
    rows = db.get_wellness_for_range(
        (today - timedelta(days=4)).isoformat(),
        today.isoformat(),
    )
    sleeps = [r["sleep_seconds"] / 3600 for r in rows if r.get("sleep_seconds")]
    if len(sleeps) < 3:
        return None
    avg = mean(sleeps[-3:])
    if avg < 6.5:
        return Alert(
            kind="sleep_debt",
            severity="moderate",
            message=(
                f"💤 Sleep averaging {avg:.1f}h over the last 3 nights — "
                f"below the 7h+ recovery threshold. Hard sessions will compound damage."
            ),
        )
    return None


def _ensure_alert_history(db: Database) -> None:
    db.conn.execute(
        """CREATE TABLE IF NOT EXISTS alert_history (
            kind TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            severity TEXT,
            message TEXT,
            PRIMARY KEY (kind, sent_at)
        )"""
    )
    db.conn.commit()


def _within_cooldown(db: Database, kind: str) -> bool:
    cutoff = (date.today() - timedelta(days=COOLDOWN_DAYS)).isoformat()
    row = db.conn.execute(
        "SELECT 1 FROM alert_history WHERE kind = ? AND sent_at >= ?",
        (kind, cutoff),
    ).fetchone()
    return row is not None


def _record_alerts(db: Database, alerts: list[Alert]) -> None:
    today_str = date.today().isoformat()
    for a in alerts:
        db.conn.execute(
            "INSERT OR IGNORE INTO alert_history (kind, sent_at, severity, message) VALUES (?, ?, ?, ?)",
            (a.kind, today_str, a.severity, a.message),
        )
    db.conn.commit()


def run_checks() -> int:
    """Evaluate all signals, send a combined message for any that fire (and aren't
    in cooldown), record what was sent. Returns the count of alerts sent."""
    db = Database(DB_PATH)
    try:
        _ensure_alert_history(db)

        checks = [check_acr, check_rhr, check_hrv, check_sleep]
        firing: list[Alert] = []
        for fn in checks:
            try:
                alert = fn(db)
            except Exception as e:
                log.warning("Alert check %s crashed: %s", fn.__name__, e)
                continue
            if alert and not _within_cooldown(db, alert.kind):
                firing.append(alert)

        if not firing:
            log.info("No alerts firing.")
            return 0

        from .telegram_bot import send_coaching_message
        body = "\n\n".join(a.message for a in firing)
        send_coaching_message(f"⚡ Daily wellness check\n\n{body}")
        _record_alerts(db, firing)
        log.info("Sent %d alert(s): %s", len(firing), [a.kind for a in firing])
        return len(firing)
    finally:
        db.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    run_checks()
