import asyncio
import html
import logging
import re
from datetime import date
from pathlib import Path

from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

from .config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ANTHROPIC_API_KEY, TRAINING_PLAN_PATH, DB_PATH, RACE_DATE, PLAN_START_DATE
from .db import Database
from .training_plan import TrainingPlan
from .coach import Coach, compute_adherence, compute_weekly_target
from .retry import with_retry

log = logging.getLogger(__name__)


# --- Telegram-flavored markdown translation ---
#
# The LLM emits standard markdown (**bold**, ---, `code`) but Telegram has
# no built-in parser for `**bold**` syntax — its MarkdownV2 uses single
# asterisks. Without translation, asterisks and dashes show literally
# (see screenshot from May 14). We translate to HTML and send with
# parse_mode=HTML. HTML special chars are escaped first so e.g. "drift >5%"
# renders as ">5%" not a broken tag.

_BOLD_RE = re.compile(r"\*\*([^*\n]+?)\*\*")
_CODE_RE = re.compile(r"`([^`\n]+?)`")
_SEP_RE = re.compile(r"^[ \t]*-{3,}[ \t]*$", re.MULTILINE)
_BLANK_RUN_RE = re.compile(r"\n{3,}")
TELEGRAM_MAX_LEN = 4096


def format_for_telegram(text: str) -> str:
    """Translate LLM markdown into Telegram HTML for nicer rendering.

    Conversions:
      **bold**         → <b>bold</b>
      `code`           → <code>code</code>
      --- (own line)   → removed (the surrounding bold section headers
                          already make structure visible; the dashes were
                          showing as literal text and looked ugly).
    Escapes <, >, & first so the LLM emitting "drift >5%" survives intact.
    """
    text = html.escape(text)
    text = _BOLD_RE.sub(r"<b>\1</b>", text)
    text = _CODE_RE.sub(r"<code>\1</code>", text)
    text = _SEP_RE.sub("", text)
    # After dropping --- lines we can be left with 3+ consecutive newlines;
    # collapse to at most 2 for tighter spacing.
    text = _BLANK_RUN_RE.sub("\n\n", text)
    return text.strip()


def _truncate_for_telegram(text: str) -> str:
    """Trim to Telegram's 4096-char limit. Done after formatting so the
    final HTML-rendered length is what's measured."""
    if len(text) <= TELEGRAM_MAX_LEN:
        return text
    return text[: TELEGRAM_MAX_LEN - 3] + "..."


# --- Proactive messaging (called from poller) ---

async def _send_message(text: str):
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    formatted = _truncate_for_telegram(format_for_telegram(text))
    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID, text=formatted, parse_mode=ParseMode.HTML
    )


def send_coaching_message(text: str):
    """Synchronous wrapper to send a Telegram message, with retry on transient failures.

    On final failure, raises the last exception so the caller (poller) can log it
    and fire its own error alert. Retries are best-effort cover for 502s and the
    occasional Telegram/network blip.
    """
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            asyncio.run(_send_message(text))
            return
        except Exception as e:
            last_err = e
            if attempt < 2:
                log.warning(
                    "Telegram send attempt %d/3 failed: %r (retry in %.1fs)",
                    attempt + 1, e, 1.0 * (2 ** attempt),
                )
                import time as _time
                _time.sleep(1.0 * (2 ** attempt))
    # All attempts exhausted — re-raise so poller surfaces it via send_error_alert.
    log.warning("Telegram send failed after 3 attempts: %r", last_err)
    raise last_err  # type: ignore[misc]


def send_error_alert(error: str):
    """Send an error alert to Telegram. Formatting + parse_mode happen inside
    _send_message so any `<` / `>` / `&` in the exception text survives."""
    msg = f"[AI Coach Error]\n\n{error}"
    try:
        asyncio.run(_send_message(msg))
    except Exception:
        pass  # Don't crash if we can't send the alert


async def _send_document(path: Path, caption: str = ""):
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    with path.open("rb") as f:
        await bot.send_document(chat_id=TELEGRAM_CHAT_ID, document=f, filename=path.name, caption=caption)


def send_backup_to_telegram(path: Path, caption: str = "") -> None:
    """Synchronous wrapper to send a file (the DB backup) as a Telegram document.

    Best-effort with 3-attempt retry; logs warnings and returns None on final
    failure (a missed backup is recoverable — it'll retry on the next activity
    or the daily 02:00 timer).
    """
    with_retry(
        lambda: asyncio.run(_send_document(path, caption)),
        _label=f"backup upload {path.name}",
    )


# --- Interactive bot ---


async def _reply(update: Update, text: str):
    """Send a reply through the same formatting pipeline as proactive messages."""
    formatted = _truncate_for_telegram(format_for_telegram(text))
    await update.message.reply_text(formatted, parse_mode=ParseMode.HTML)


class CoachBot:
    def __init__(self):
        self.db = Database(DB_PATH)
        self.plan = TrainingPlan(str(TRAINING_PLAN_PATH))
        self.coach = Coach(ANTHROPIC_API_KEY, self.plan, self.db)

    def _is_authorized(self, update: Update) -> bool:
        if str(update.effective_chat.id) != TELEGRAM_CHAT_ID:
            return False
        self.plan.reload_if_changed()
        return True

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        await _reply(
            update,
            "**AI Running Coach active!**\n\n"
            "Commands:\n"
            "/today - What's prescribed today\n"
            "/week - This week's plan\n"
            "/status - Recent training summary\n"
            "/lastrun - Re-send the most recent run analysis\n"
            "/backup - Grab a fresh DB backup\n\n"
            "Or just send me a message to chat about your training!",
        )

    async def cmd_today(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        today = date.today()
        prescribed = self.plan.get_prescribed_run(today)
        week = self.plan.get_week_for_date(today)

        if prescribed:
            msg = (
                f"**Week {week.week_number} ({week.phase})** — {today.strftime('%A, %b %d')}\n\n"
                f"**Today's workout:**\n{prescribed.description}"
            )
            if week.notes:
                msg += f"\n\n**Notes:** {week.notes}"
        elif week:
            day_labels = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
            today_slot = week.day(today.weekday())
            next_runs = [
                f"{day_labels[i]}: {week.day(i).description}"
                for i in range(today.weekday() + 1, 7)
                if week.day(i).workout_type != "rest"
            ]

            msg = f"**Week {week.week_number} ({week.phase})** — {today.strftime('%A, %b %d')}\n\n"
            # Show today's non-running prescription verbatim (e.g. strength,
            # travel, planned rest) — surfacing it is more useful than a
            # generic "rest up" string.
            if today_slot.description and today_slot.description.lower() != "rest":
                msg += f"**Today:** {today_slot.description}\n\n"
            else:
                msg += "No run today — rest up!\n\n"
            if next_runs:
                msg += "**Coming up:**\n" + "\n".join(next_runs)
            else:
                msg += "You've finished this week's runs. Recover well!"
        else:
            msg = "You're outside the training plan period."
        await _reply(update, msg)

    async def cmd_week(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        week = self.plan.get_week_for_date(date.today())
        if week:
            msg = self.plan.get_week_summary(week)
        else:
            msg = "No training week found for today."
        await _reply(update, msg)

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return

        # Race countdown header
        countdown = self.coach._race_countdown()
        countdown_text = (
            f"**Race:** {countdown['days_remaining']} days to Lisbon Marathon\n"
            f"**Plan:** Week {countdown['current_week']}/{countdown['total_weeks']} "
            f"({countdown['pct_complete']}% complete)\n"
            f"**Weeks remaining:** {countdown['weeks_remaining']}\n\n"
        )

        # Plan adherence + weekly target progress
        today = date.today()
        adherence = compute_adherence(self.plan, self.db, today)
        if adherence["total"] > 0:
            countdown_text += (
                f"**Adherence:** {adherence['completed']}/{adherence['total']} "
                f"of last {adherence['total']} prescribed runs\n"
            )
        weekly_target = compute_weekly_target(self.plan, self.db, today)
        if weekly_target:
            countdown_text += (
                f"**This week:** {weekly_target['actual_km']}/"
                f"{weekly_target['target_km']}km "
                f"({weekly_target['pct']}%, "
                f"{weekly_target['days_remaining']}d left)\n"
            )
        countdown_text += "\n"

        # Training status from Garmin
        ts = self.db.get_latest_training_status()
        if ts:
            countdown_text += (
                f"**Training load (7d):** {ts.get('training_load_7d', 'N/A')}\n"
                f"**Recovery time:** {ts.get('recovery_time_hours', 'N/A')}h\n"
                f"**VO2max:** {ts.get('vo2max', 'N/A')}\n"
                f"**Status:** {ts.get('training_status_label', 'N/A')}\n\n"
            )

        response = self.coach.chat(
            "Give me a brief training status summary based on my recent runs. "
            "How am I tracking against the plan?"
        )
        await _reply(update, countdown_text + response)

    async def cmd_lastrun(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        runs = self.db.get_recent_activities(limit=1)
        if not runs:
            await _reply(update, "No runs in the database yet.")
            return
        run = runs[0]
        text = run.get("coaching_response")
        if not text:
            await _reply(
                update,
                f"Most recent run ({run.get('start_time', '?')[:10]}, "
                f"{run.get('distance_km', 0):.1f}km) has no saved analysis — "
                f"the poller may not have processed it yet.",
            )
            return
        await _reply(update, text)

    async def cmd_backup(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        from .backup import run_backup
        try:
            path = run_backup()
        except Exception as e:
            log.exception("Backup failed")
            await _reply(update, f"Backup failed: {e}")
            return
        with path.open("rb") as f:
            await update.message.reply_document(
                document=f,
                filename=path.name,
                caption=f"DB backup ({path.stat().st_size} bytes, gzipped)",
            )

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        response = self.coach.chat(update.message.text)
        await _reply(update, response)

    def run(self):
        app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("today", self.cmd_today))
        app.add_handler(CommandHandler("week", self.cmd_week))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("lastrun", self.cmd_lastrun))
        app.add_handler(CommandHandler("backup", self.cmd_backup))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

        log.info("Telegram bot starting...")
        app.run_polling()
