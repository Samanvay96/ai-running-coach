# 🏃 AI Running Coach

A personal AI running coach that monitors your Garmin activities and delivers coaching feedback via Telegram. Built to run on a Raspberry Pi. 🥧

## ✨ What it does

- 📡 **Monitors Garmin Connect** for new running activities (polls every 2 hours)
- 🧠 **Analyzes each run** against your training plan — compares actual vs prescribed pace, distance, HR, and splits
- 💬 **Sends coaching feedback** to Telegram automatically after each detected run
- 🗣️ **Interactive chat** — ask your coach anything about your training via Telegram
- ⌨️ **Telegram commands**:
  - `/today` — what's prescribed today
  - `/week` — this week's training overview
  - `/status` — recent training summary and progress check

## 📊 Metrics it tracks

**🛌 Recovery & load (Tier 1)**
- 💤 Sleep hours + sleep score (overnight Garmin data)
- ❤️‍🩹 HRV last night, 7-day avg, Garmin status (BALANCED / UNBALANCED / etc.)
- 💗 Resting HR + 7-day trend
- ⚖️ Acute:Chronic load ratio (acute 7d / chronic 28d-weekly-equivalent — sweet spot 0.8–1.3)
- 📈 Week-over-week mileage delta vs prior 4-week average

**🏃‍♂️ Run quality (Tier 2)**
- 📉 HR drift / aerobic decoupling — 1st-vs-2nd-half pace + HR comparison
- 🎯 Z2 time-in-zone % — derived via Karvonen / %HRR using your max HR (220−age) and latest overnight RHR
- 🩸 Lactate threshold pace + HR (Garmin estimate)
- 🪜 Per-km splits with HR per km

**🗓️ Plan context**
- Current training week, phase, and prescribed run for today
- Pace zones from the Pace Guide sheet
- Race countdown and benchmark targets
- Plan reloads on every Telegram command — no restart needed when you edit the xlsx

## 🏗️ Architecture

Two background services on a Raspberry Pi:

```
Garmin Connect ──(every 2h)──> Poller ──(new run?)──> LLM Analysis ──> Telegram
                                  │                        ↑
                                  ├── SQLite ←── Training Plan (Excel)
                                  └── Daily wellness (sleep / HRV / RHR)

Telegram user ──(message)──> Bot ──> LLM Chat ──> Reply
```

## 📋 Prerequisites

- 🐍 Python 3.12+
- ⌚ A [Garmin Connect](https://connect.garmin.com/) account with a Garmin watch (worn at least overnight for HRV / RHR / sleep)
- 🤖 A [Telegram bot](https://core.telegram.org/bots#botfather) (create one via @BotFather)
- 🔑 An [Anthropic API key](https://console.anthropic.com/)
- 📑 A training plan in Excel format (see below)

## 🚀 Setup

### 1️⃣ Clone the repo

```bash
git clone git@github.com:Samanvay96/ai-running-coach.git
cd ai-running-coach
```

### 2️⃣ Create your environment file

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
RUNNER_AGE=30
```

> 💡 **Getting your Telegram chat ID:** Send a message to your bot, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates` — your chat ID is in the response.
>
> 🫀 **`RUNNER_AGE`** is used to derive `MAX_HR = 220 − age` for Z2 zone math (Karvonen / %HRR formula). The 220−age estimate is approximate (±10 bpm typical).

### 3️⃣ Add your training plan

Place your training plan Excel file in the project root. The parser expects an `.xlsx` file with three sheets:

- 📋 **Training Plan** — week-by-week schedule with columns: `Week`, `Dates`, `Phase`, `Mon`, `Tue`, `Thu`, `Sat`, `Weekly km`, `Notes`
- 🎚️ **Pace Guide** — pace zones with heart rate targets (e.g. `Zone 2 (60-70% max HR)`)
- 🏁 **Race Day** — pacing strategy and fueling plan

Update `TRAINING_PLAN_PATH` in `src/config.py` if your filename differs.

> ✏️ Edits to the xlsx are picked up automatically — the bot reloads the plan on every command via mtime check.

### 4️⃣ Run the setup script

```bash
./setup.sh
```

This creates a virtual environment, installs dependencies, and installs systemd services.

### 5️⃣ Start the services

```bash
sudo systemctl start ai-coach-bot          # 🤖 Telegram bot
sudo systemctl start ai-coach-poll.timer   # 📡 Garmin poller
```

Both services auto-start on boot.

## 🛠️ Usage

### 🩺 Check service status

```bash
sudo systemctl status ai-coach-bot
sudo systemctl status ai-coach-poll.timer
```

### 📜 View logs

```bash
journalctl -u ai-coach-bot -f         # Bot logs
journalctl -u ai-coach-poll -f        # Poller logs
```

### ⚡ Run the poller manually

```bash
.venv/bin/python -m src.poller
```

### ⏱️ Adjust polling interval

Edit `systemd/ai-coach-poll.timer`, change `OnUnitActiveSec`, then:

```bash
sudo cp systemd/ai-coach-poll.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart ai-coach-poll.timer
```

## 📁 Project Structure

```
├── 🔐 .env.example          # Template for credentials
├── 📦 requirements.txt      # Python dependencies
├── 🔧 setup.sh              # One-time setup script
├── 📂 src/
│   ├── ⚙️  config.py         # Environment config and constants
│   ├── 💾 db.py             # SQLite database layer
│   ├── 📋 training_plan.py  # Excel training plan parser
│   ├── ⌚ garmin_client.py  # Garmin Connect API wrapper
│   ├── 🧠 coach.py          # LLM coaching engine
│   ├── 💬 telegram_bot.py   # Telegram bot handlers
│   ├── 🔄 poller.py         # New run detection and analysis
│   └── 🚪 main.py           # Bot entry point
└── 🛠️  systemd/              # systemd service and timer units
```

## 🧪 Tech Stack

- 🐍 **Python 3** with `garminconnect`, `python-telegram-bot`, `anthropic`, `openpyxl`
- 💾 **SQLite** for activity history, daily wellness, session tokens, and conversation state
- ⚙️ **systemd** for process management on Raspberry Pi
- 🤖 **Anthropic Claude (Sonnet 4)** for run analysis and chat
