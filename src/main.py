#!/usr/bin/env python3
"""Entry point for the Telegram bot (long-running process)."""

import logging
import traceback

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

from src.telegram_bot import CoachBot, send_error_alert

if __name__ == "__main__":
    try:
        bot = CoachBot()
        bot.run()
    except Exception as e:
        log.error("Bot crashed: %s", e)
        send_error_alert(f"Bot crashed and will restart:\n{traceback.format_exc()[-500:]}")
        raise
