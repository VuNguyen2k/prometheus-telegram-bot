# Prometheus Telegram Bot

A lightweight Telegram bot that queries a Prometheus server for system metrics (CPU, RAM, Disk, GPU) and sends a compact summary via the `/latest` command.

## Prerequisites

* A running **Prometheus** server with **node_exporter** (CPU/RAM/Disk) and optionally **DCGM exporter** (GPU).
* A **Telegram Bot** token (create one via [@BotFather](https://t.me/BotFather)).
* The **chat/channel ID** where the bot should post.

## Setup

```bash
cd prometheus-telegram-bot
cp .env.template .env
# Edit .env with your values
```

### Run locally

```bash
pip install -r requirements.txt
python bot.py
```

### Run with Docker

```bash
docker build -t prom-tgbot .
docker run --rm --env-file .env prom-tgbot
```

## Environment Variables

| Variable | Description |
|---|---|
| `PROMETHEUS_TARGETS` | Comma-separated `name=url` pairs (e.g. `server1=http://10.0.0.1:9090,server2=http://10.0.0.2:9090`) |
| `TELEGRAM_BOT_TOKEN` | Bot API token from BotFather |
| `TELEGRAM_CHANNEL_ID` | Target chat or channel ID |

## Usage

Send `/latest` to the bot in the configured chat. It will reply with a monospace block like:

```
--- server1 ---
CPU: 45.2% | Temp: 65.0C | Fan: 1200 RPM
RAM: 16.2GB / 32.0GB (51%)
Disk /: 45.0GB / 100.0GB (45%)
GPU 0: 80% | VRAM: 12/24GB | Temp: 72C | Fan: 60%

--- server2 ---
CPU: 12.0% | Temp: 42.0C | Fan: 900 RPM
RAM: 8.1GB / 64.0GB (13%)
Disk /: 120.0GB / 500.0GB (24%)
GPU: N/A
```

Missing metrics (e.g. no fan sensor) display as `N/A`.
