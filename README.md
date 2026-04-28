# 🏃 AI Running Coach

A personal AI running coach that monitors your Garmin activities and delivers coaching feedback via Telegram. Built to run on a Raspberry Pi. 🥧

## ✨ What it does

- 📡 **Monitors Garmin Connect** for new running activities (polls every 2 hours)
- 🧠 **Analyzes each run** against your training plan — compares actual vs prescribed pace, distance, HR, and splits
- 💬 **Sends coaching feedback** to Telegram automatically after each detected run
- 🗣️ **Interactive chat** — ask your coach anything about your training via Telegram
- 🚨 **Proactive overload alerts** — daily morning check for ACR creep, RHR drift, HRV suppression, sleep debt; nudges you *before* you overdo it
- ⚠️ **Failure alerts** — if a run analysis fails to generate or deliver, the poller surfaces the exception (with type and `stop_reason`) to Telegram so you notice in seconds, not the next time you check the journal
- ☁️ **Auto off-Pi backups** — every new run triggers a fresh DB backup that's sent to Telegram, so your training history survives an SD card failure
- ⌨️ **Telegram commands**:
  - `/today` — what's prescribed today
  - `/week` — this week's training overview
  - `/status` — recent training summary and progress check
  - `/backup` — grab a fresh DB backup as a Telegram document attachment

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

Four scheduled jobs + one always-on bot, all running under systemd on a Raspberry Pi:

```
Garmin Connect ──(every 2h)──> Poller ──(new run?)──> LLM Analysis ──> Telegram
                                  │                        ↑
                                  ├── SQLite ←── Training Plan (Excel)
                                  └── Daily wellness (sleep / HRV / RHR)
                                          ↓
                                          ├──(after each run)── Backup → Telegram
                                          ↓
       (daily 02:00) ─────── Backup timer ──→ data/backups/coach-YYYYMMDD.db.gz
       (daily 08:00) ─────── Alerts timer ──→ Combined wellness nudge → Telegram

Telegram user ──(message / /command)──> Bot ──> LLM Chat ──> Reply
```

All Garmin API calls have automatic retry-with-backoff (3 attempts), so transient
failures don't leave gaps in your wellness or load history.

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
sudo systemctl start ai-coach-bot              # 🤖 Telegram bot (always-on)
sudo systemctl start ai-coach-poll.timer       # 📡 Garmin poller (every 2h)
sudo systemctl enable --now ai-coach-backup.timer ai-coach-alerts.timer
                                                # 💾 Daily 02:00 backup + 08:00 wellness alerts
```

All services auto-start on boot.

### 6️⃣ (Optional) Backfill historical wellness data

If you've been wearing your watch overnight before installing the bot, pull
the last N days of sleep / HRV / RHR from Garmin Connect so the alert checks
have a full baseline from day one:

```bash
.venv/bin/python -m src.backfill_wellness 60   # backfill 60 days (default 30)
```

Idempotent — safe to re-run.

## 🛠️ Usage

### 🩺 Check service status

```bash
sudo systemctl status ai-coach-bot
sudo systemctl status ai-coach-poll.timer
sudo systemctl status ai-coach-backup.timer
sudo systemctl status ai-coach-alerts.timer
systemctl list-timers ai-coach-*.timer       # see all next-fire times at a glance
```

### 📜 View logs

```bash
journalctl -u ai-coach-bot -f         # Bot logs
journalctl -u ai-coach-poll -f        # Poller logs
journalctl -u ai-coach-backup -f      # Backup logs
journalctl -u ai-coach-alerts -f      # Alert logs
```

### ⚡ Run any job manually

```bash
.venv/bin/python -m src.poller                # Poll now
.venv/bin/python -m src.backup                # Run a backup now
.venv/bin/python -m src.alerts                # Run wellness check now
.venv/bin/python -m src.backfill_wellness 30  # Backfill 30 days of overnight data
.venv/bin/python -m scripts.replay_analyze <activity_id> --deliver
                                              # Re-run analysis for a stored activity, save, and send to Telegram
```

### ⏱️ Adjust polling interval

Edit `systemd/ai-coach-poll.timer`, change `OnUnitActiveSec`, then:

```bash
sudo cp systemd/ai-coach-poll.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart ai-coach-poll.timer
```

## 🚨 What the alerts watch for

The daily 08:00 wellness check sends a single combined Telegram nudge if any of
these signals fire (each has a 3-day cooldown so multi-day overreach doesn't spam
you):

| Check | Threshold | Why |
|---|---|---|
| ACR | acute:chronic load ratio ≥ 1.5 | Sweet spot is 0.8–1.3; >1.5 is the standard injury-risk window |
| RHR creep | last 3 nights avg ≥ prior 11-night baseline + 5 bpm | Classic overreach / illness-brewing signal |
| HRV drop | 3 nights all `UNBALANCED` OR ≥15% below 7-day avg | Confirms autonomic stress |
| Sleep debt | 3-night avg < 6.5h | Below recovery threshold; hard runs will compound damage |

## 💾 Backup behaviour

- 🕑 **Daily 02:00** — a full snapshot of `data/coach.db` lands in `data/backups/coach-YYYYMMDD.db.gz` (rolling 14-day retention).
- 🏃 **After each new run** — same snapshot is also uploaded to Telegram as a document attachment, giving you an off-Pi copy automatically.
- 🪛 **`/backup` command** — manually triggers the same flow on demand (useful before travel or after data corrections).

Snapshots use SQLite's `.backup()` API, so they're atomic even while the bot is writing.

## 📁 Project Structure

```
├── 🔐 .env.example                # Template for credentials
├── 📦 requirements.txt            # Python dependencies
├── 🔧 setup.sh                    # One-time setup script
├── 📂 src/
│   ├── ⚙️  config.py               # Environment config and constants
│   ├── 💾 db.py                   # SQLite database layer
│   ├── 📋 training_plan.py        # Excel training plan parser (auto-reload on edit)
│   ├── ⌚ garmin_client.py        # Garmin Connect API wrapper (with retries)
│   ├── 🧠 coach.py                # LLM coaching engine + metric helpers
│   ├── 💬 telegram_bot.py         # Telegram bot handlers + commands
│   ├── 🔄 poller.py               # New run detection and analysis
│   ├── 💾 backup.py               # SQLite atomic snapshot + rotation
│   ├── 🚨 alerts.py               # Daily proactive overload checks
│   ├── 🌙 backfill_wellness.py    # One-shot wellness history loader
│   └── 🚪 main.py                 # Bot entry point
├── 🧰 scripts/                    # One-off diagnostics (replay an activity's analysis, etc.)
└── 🛠️  systemd/                    # systemd service + timer units
```

## 🧪 Tech Stack

- 🐍 **Python 3** with `garminconnect`, `python-telegram-bot`, `anthropic`, `openpyxl`
- 💾 **SQLite** for activity history, daily wellness, session tokens, conversation state, and alert dedup
- ⚙️ **systemd** for process management on Raspberry Pi (4 timers + 1 always-on service)
- 🤖 **Anthropic Claude (Sonnet 4.6)** with adaptive thinking for run analysis and weekly summaries
