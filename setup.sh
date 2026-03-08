#!/bin/bash
set -e

PROJ_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== AI Running Coach Setup ==="

# Check .env exists
if [ ! -f "$PROJ_DIR/.env" ]; then
    echo "ERROR: .env file not found. Copy .env.example to .env and fill in your credentials."
    echo "  cp .env.example .env"
    exit 1
fi

# Create venv if needed
if [ ! -d "$PROJ_DIR/.venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$PROJ_DIR/.venv"
fi

# Install dependencies
echo "Installing dependencies..."
"$PROJ_DIR/.venv/bin/pip" install -r "$PROJ_DIR/requirements.txt" --quiet

# Create data directory
mkdir -p "$PROJ_DIR/data"

# Install systemd units
echo "Installing systemd services..."
sudo cp "$PROJ_DIR/systemd/ai-coach-bot.service" /etc/systemd/system/
sudo cp "$PROJ_DIR/systemd/ai-coach-poll.service" /etc/systemd/system/
sudo cp "$PROJ_DIR/systemd/ai-coach-poll.timer" /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable ai-coach-bot.service
sudo systemctl enable ai-coach-poll.timer

echo ""
echo "Setup complete! To start:"
echo "  sudo systemctl start ai-coach-bot     # Start Telegram bot"
echo "  sudo systemctl start ai-coach-poll.timer  # Start polling timer"
echo ""
echo "To check status:"
echo "  sudo systemctl status ai-coach-bot"
echo "  sudo systemctl status ai-coach-poll.timer"
echo "  journalctl -u ai-coach-bot -f          # Follow bot logs"
echo "  journalctl -u ai-coach-poll -f         # Follow poller logs"
