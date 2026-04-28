import asyncio
import logging
from datetime import date

from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

from .config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ANTHROPIC_API_KEY, TRAINING_PLAN_PATH, DB_PATH, RACE_DATE, PLAN_START_DATE
from .db import Database
from .training_plan import TrainingPlan
from .coach import Coach

log = logging.getLogger(__name__)


# --- Proactive messaging (called from poller) ---

async def _send_message(text: str):
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    # Telegram message limit is 4096 chars
    if len(text) > 4096:
        text = text[:4093] + "..."
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)


def send_coaching_message(text: str):
    """Synchronous wrapper to send a Telegram message."""
    asyncio.run(_send_message(text))


def send_error_alert(error: str):
    """Send an error alert to Telegram."""
    msg = f"[AI Coach Error]\n\n{error}"
    if len(msg) > 4096:
        msg = msg[:4093] + "..."
    try:
        asyncio.run(_send_message(msg))
    except Exception:
        pass  # Don't crash if we can't send the alert


# --- Interactive bot ---

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
        await update.message.reply_text(
            "AI Running Coach active!\n\n"
            "Commands:\n"
            "/today - What's prescribed today\n"
            "/week - This week's plan\n"
            "/status - Recent training summary\n\n"
            "Or just send me a message to chat about your training!"
        )

    async def cmd_today(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        today = date.today()
        prescribed = self.plan.get_prescribed_run(today)
        week = self.plan.get_week_for_date(today)

        if prescribed:
            msg = (
                f"Week {week.week_number} ({week.phase}) - {today.strftime('%A, %b %d')}\n\n"
                f"Today's workout:\n{prescribed.description}\n\n"
                f"Notes: {week.notes}"
            )
        else:
            if week:
                weekday = today.strftime("%A")
                # Show what's coming next
                next_runs = []
                if today.weekday() < 1:
                    next_runs.append(f"Tue: {week.tuesday.description}")
                if today.weekday() < 3:
                    next_runs.append(f"Thu: {week.thursday.description}")
                if today.weekday() < 5:
                    next_runs.append(f"Sat: {week.saturday.description}")

                msg = f"Week {week.week_number} ({week.phase}) - {today.strftime('%A, %b %d')}\n\n"
                msg += f"No run today — rest up!\n\n"
                if today.weekday() == 0:
                    msg += f"Cross-training: {week.monday.description}\n\n"
                if next_runs:
                    msg += "Coming up:\n" + "\n".join(next_runs)
                else:
                    msg += "You've finished this week's runs. Recover well!"
            else:
                msg = "You're outside the training plan period."
        await update.message.reply_text(msg)

    async def cmd_week(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        week = self.plan.get_week_for_date(date.today())
        if week:
            msg = self.plan.get_week_summary(week)
        else:
            msg = "No training week found for today."
        await update.message.reply_text(msg)

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return

        # Race countdown header
        countdown = self.coach._race_countdown()
        countdown_text = (
            f"Race: {countdown['days_remaining']} days to Lisbon Marathon\n"
            f"Plan: Week {countdown['current_week']}/{countdown['total_weeks']} "
            f"({countdown['pct_complete']}% complete)\n"
            f"Weeks remaining: {countdown['weeks_remaining']}\n\n"
        )

        # Training status from Garmin
        ts = self.db.get_latest_training_status()
        if ts:
            countdown_text += (
                f"Training load (7d): {ts.get('training_load_7d', 'N/A')}\n"
                f"Recovery time: {ts.get('recovery_time_hours', 'N/A')}h\n"
                f"VO2max: {ts.get('vo2max', 'N/A')}\n"
                f"Status: {ts.get('training_status_label', 'N/A')}\n\n"
            )

        response = self.coach.chat(
            "Give me a brief training status summary based on my recent runs. "
            "How am I tracking against the plan?"
        )
        await update.message.reply_text(countdown_text + response)

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        response = self.coach.chat(update.message.text)
        if len(response) > 4096:
            response = response[:4093] + "..."
        await update.message.reply_text(response)

    def run(self):
        app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("today", self.cmd_today))
        app.add_handler(CommandHandler("week", self.cmd_week))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

        log.info("Telegram bot starting...")
        app.run_polling()
