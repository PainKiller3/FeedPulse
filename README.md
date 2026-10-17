# ⚡ FeedPulse - Realtime RSS & Torrent Notifier Bot

> **FeedPulse** is a dual-platform bot for **Telegram** and **Discord** that monitors RSS and BitTorrent feeds in real-time. Whenever a new torrent or feed item is uploaded, it automatically delivers rich notifications featuring **file size**, **seeders/leechers**, direct **`.torrent` download links**, and **1-click Magnet URIs**.

---

## ✨ Features

- 🌊 **BitTorrent Enriched**: Automatically detects torrent feeds, extracts infohashes, seeders, leechers, file size, and generates instant `magnet:?xt=urn:btih:...` links with public trackers.
- 🤖 **Dual-Platform (Telegram & Discord)**: Run Telegram, Discord, or both simultaneously from a single process.
- ⚡ **Realtime Background Poller**: Asynchronous polling with HTTP conditional GET headers (`ETag` and `Last-Modified`) to minimize bandwidth and stay polite to feed servers.
- 🔍 **Keyword & Exclusion Filters**: Filter any feed by including only matching titles (e.g., `1080p, FLAC`) or excluding keywords (e.g., `CAM, Hindi`).
- 🛡 **Smart Deduplication & Spam Protection**: Automatically records baseline items when a new feed is added so you don't get spammed with 50 old uploads.
- 🗄 **Embedded SQLite Storage**: Lightweight, asynchronous SQLite database using `aiosqlite` with automatic cleanup of old items.
- 🐳 **Docker & Docker Compose Ready**: Easily self-host 24/7 on any VPS, local machine, or home lab.

---

## 📋 Commands Reference

Both **Telegram** and **Discord** (slash commands + prefix `!`) support the exact same commands:

| Command | Description | Example |
| :--- | :--- | :--- |
| `/start` or `/help` | Displays help message and quick-start guide | `/help` |
| `/add <url>` | Subscribe current chat/channel to an RSS feed | `/add https://example.com/rss/feed.xml` |
| `/list` | List all feeds monitored in this chat/channel | `/list` |
| `/remove <id>` | Unsubscribe and stop monitoring a feed | `/remove 1` |
| `/filter <id> <rules>` | Configure keyword inclusion/exclusion rules | `/filter 1 inc=1080p,flac exc=cam,sample` |
| `/check` | Force an immediate check on active feeds | `/check` |
| `/test` | Send a preview torrent alert to test formatting | `/test` |

---

## 🚀 Quick Start Guide

### 1. Clone & Install Dependencies

Ensure you have **Python 3.10+** installed:

```bash
git clone https://github.com/PainKiller3/FeedPulse.git
cd FeedPulse
pip install -r requirements.txt
```

### 2. Configure Settings (`config.py`)

Copy `config_sample.py` to `config.py`:

```bash
cp config_sample.py config.py
```

Edit `config.py` and set your bot token(s):

```python
# Telegram Bot Token (from @BotFather) - optional if Discord only
TELEGRAM_BOT_TOKEN = "123456789:ABCdefGhIJKlmNoPQRstuVWXyz"

# Discord Bot Token (from Discord Developer Portal) - optional if Telegram only
DISCORD_BOT_TOKEN = "MTE5OTg..."

# Polling frequency in seconds (default: 120s = 2 minutes)
POLL_INTERVAL_SECONDS = 120
```

### 3. Run the Bot

```bash
python main.py
```

---

## 🤖 Platform Setup Guides

### 📱 Telegram Bot Setup

1. Open Telegram and search for [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and follow the prompts to choose a display name and username (e.g. `MyFeedPulseBot`).
3. Copy the **HTTP API Token** provided by BotFather into `TELEGRAM_BOT_TOKEN` in your `config.py`.
4. *(Optional for Group Chats)*:
   - In @BotFather, send `/setprivacy` -> Select your bot -> Choose **Disable** (this allows the bot to read `/add` and other commands inside group chats).
   - Add your bot to any Telegram group or channel and promote it to Admin so it can send messages.

---

### 🎮 Discord Bot Setup

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications).
2. Click **New Application** and give it a name (e.g., `FeedPulse`).
3. Go to the **Bot** tab on the left menu:
   - Click **Reset Token** to get your token and paste it into `DISCORD_BOT_TOKEN` in `config.py`.
   - Scroll down to **Privileged Gateway Intents** and enable **Message Content Intent**.
4. Go to **OAuth2 -> URL Generator**:
   - Under **Scopes**, select: `bot` and `applications.commands`.
   - Under **Bot Permissions**, select:
     - `Send Messages`
     - `Embed Links`
     - `Attach Files`
     - `Read Message History`
5. Copy the generated invite URL at the bottom, paste it into your browser, and add the bot to your Discord server!

---

## 🔍 Keyword Filtering Examples

You can fine-tune what torrents trigger alerts:

- **Only alert on specific quality / formats:**
  ```text
  /filter 1 inc=1080p,flac,2160p
  ```
- **Block unwanted releases:**
  ```text
  /filter 1 exc=cam,telesync,hindi,sample
  ```
- **Combine both:**
  ```text
  /filter 1 inc=remux,bluray exc=cam,ts
  ```
- **Clear filters:**
  ```text
  /filter 1 clear
  ```

---

## 🐳 Running with Docker

Run FeedPulse in the background with automatic restart using Docker Compose:

```bash
# 1. Configure config.py with your tokens
# 2. Start container
docker-compose up -d

# Check logs
docker-compose logs -f
```

---

## 📂 Project Architecture

```text
FeedPulse/
├── config_sample.py      # Configuration template file
├── main.py               # Unified application runner & lifecycle manager
├── database/
│   ├── __init__.py
│   └── db.py             # Asynchronous SQLite storage & indexing (aiosqlite)
├── parser/
│   ├── __init__.py
│   └── feed_parser.py    # RSS & BitTorrent metadata extractor + Magnet builder
├── services/
│   ├── __init__.py
│   ├── dispatcher.py     # Multi-platform notification formatter (HTML & Embeds)
│   └── poller.py         # Async scheduler loop with conditional GET polling
├── bots/
│   ├── __init__.py
│   ├── telegram_bot.py   # Telegram Bot implementation (python-telegram-bot v22)
│   └── discord_bot.py    # Discord Bot implementation with Slash & Prefix commands
├── Dockerfile            # Container build recipe
├── docker-compose.yml    # Compose orchestration with persistent SQLite volume
└── requirements.txt      # Python dependencies
```

---

## 📄 License

MIT License. Free to use, modify, and distribute.
