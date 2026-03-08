# AI Running Coach

A personal AI running coach that monitors your Garmin activities and delivers coaching feedback via Telegram. Built to run on a Raspberry Pi.

## What it does

- **Monitors Garmin Connect** for new running activities (polls every 2 hours)
- **Analyzes each run** against your training plan — compares actual vs prescribed pace, distance, heart rate, and splits
- **Sends coaching feedback** to Telegram automatically after each detected run
- **Interactive chat** — ask your coach anything about your training via Telegram
- **Telegram commands**:
  - `/today` — what's prescribed today
  - `/week` — this week's training overview
  - `/status` — recent training summary and progress check

## Architecture

Two background services running on a Raspberry Pi:

```
Garmin Connect ──(every 2h)──> Poller ──(new run?)──> LLM Analysis ──> Telegram
                                  │                        ↑
                                  └── SQLite ←── Training Plan (Excel)

Telegram user ──(message)──> Bot ──> LLM Chat ──> Reply
```

## Prerequisites

- Python 3.12+
- A [Garmin Connect](https://connect.garmin.com/) account with a Garmin watch
- A [Telegram bot](https://core.telegram.org/bots#botfather) (create one via @BotFather)
- An [Anthropic API key](https://console.anthropic.com/)
- A training plan in Excel format (see below)

## Setup

### 1. Clone the repo

```bash
git clone git@github.com:Samanvay96/ai-running-coach.git
cd ai-running-coach
```

### 2. Create your environment file

```bash
cp .env.example .env
chmod 600 .env
```

Edit `.env` with your credentials:

```
GARMIN_EMAIL=your@email.com
GARMIN_PASSWORD=your_garmin_password
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_CHAT_ID=your_numeric_chat_id
ANTHROPIC_API_KEY=sk-ant-...
```

**Getting your Telegram chat ID:** Send a message to your bot, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates` — your chat ID is in the response.

### 3. Add your training plan

Place your training plan Excel file in the project root. The parser expects an `.xlsx` file with three sheets:

- **Training Plan** — week-by-week schedule with columns: Week, Dates, Phase, Mon, Tue, Thu, Sat, Weekly km, Notes
- **Pace Guide** — pace zones with heart rate targets
- **Race Day** — pacing strategy and fueling plan

Update `TRAINING_PLAN_PATH` in `src/config.py` if your filename differs.

### 4. Run the setup script

```bash
./setup.sh
```

This creates a virtual environment, installs dependencies, and installs systemd services.

### 5. Start the services

```bash
sudo systemctl start ai-coach-bot         # Telegram bot
sudo systemctl start ai-coach-poll.timer   # Garmin poller
```

Both services auto-start on boot.

## Usage

### Check service status

```bash
sudo systemctl status ai-coach-bot
sudo systemctl status ai-coach-poll.timer
```

### View logs

```bash
journalctl -u ai-coach-bot -f         # Bot logs
journalctl -u ai-coach-poll -f        # Poller logs
```

### Run the poller manually

```bash
.venv/bin/python -m src.poller
```

### Adjust polling interval

Edit `systemd/ai-coach-poll.timer`, change `OnUnitActiveSec`, then:

```bash
sudo cp systemd/ai-coach-poll.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart ai-coach-poll.timer
```

## Project Structure

```
├── .env.example          # Template for credentials
├── requirements.txt      # Python dependencies
├── setup.sh              # One-time setup script
├── src/
│   ├── config.py         # Environment config and constants
│   ├── db.py             # SQLite database layer
│   ├── training_plan.py  # Excel training plan parser
│   ├── garmin_client.py  # Garmin Connect API wrapper
│   ├── coach.py          # LLM coaching engine
│   ├── telegram_bot.py   # Telegram bot handlers
│   ├── poller.py         # New run detection and analysis
│   └── main.py           # Bot entry point
└── systemd/              # systemd service and timer units
```

## Tech Stack

- **Python 3** with `garminconnect`, `python-telegram-bot`, `anthropic`, `openpyxl`
- **SQLite** for activity history, session tokens, and conversation state
- **systemd** for process management on Raspberry Pi
