#!/usr/bin/env python3
"""Entry point for the Telegram bot (long-running process)."""

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

from src.telegram_bot import CoachBot

if __name__ == "__main__":
    bot = CoachBot()
    bot.run()
