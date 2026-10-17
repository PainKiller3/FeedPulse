# ⚡ FeedPulse - Realtime RSS & Torrent Notifier Bot

> **FeedPulse** is a dual-platform bot for **Telegram** and **Discord** that monitors RSS and BitTorrent feeds in real-time. Whenever a new torrent or feed item is uploaded, it automatically delivers rich notifications featuring **file size**, **seeders/leechers**, direct **`.torrent` download links**, and **1-click Magnet URIs**.

---

## ✨ Features

- 🌊 **BitTorrent Enriched**: Automatically detects torrent feeds, extracts infohashes, seeders, leechers, file size, and generates instant `magnet:?xt=urn:btih:...` links with public trackers.
- 🌐 **Universal Feed Support (RSS, Atom, JSON Feed & TorAPI)**: In addition to standard RSS and Atom XML, natively monitors JSON feeds including TorAPI endpoints, JSON Feed v1/v1.1, and Jackett / Prowlarr API search results.
- 🤖 **Dual-Platform (Telegram & Discord)**: Run Telegram, Discord, or both simultaneously from a single process.
- ⚡ **Realtime Background Poller**: Asynchronous polling with HTTP conditional GET headers (`ETag` and `Last-Modified`) to minimize bandwidth and stay polite to feed servers.
- 🔍 **Keyword & Exclusion Filters**: Filter any feed by including only matching titles (e.g., `1080p, FLAC`) or excluding keywords (e.g., `CAM, Hindi`).
- 🛡 **Smart Deduplication & Spam Protection**: Automatically records baseline items when a new feed is added so you don't get spammed with 50 old uploads.
- 🍃 **Cloud Persistent Storage (MongoDB)**: Powered by MongoDB Atlas (`DATABASE_URL`) via `motor` for 100% persistence across restarts—ready for cloud deployment on Render, Koyeb, Railway, Heroku, or VPS.
- 🐳 **Docker & Cloud Ready**: Easily deploy 24/7 on any cloud container, VPS, or local machine.

---

## 📋 Commands Reference

Both **Telegram** and **Discord** (slash commands + prefix `!`) support the exact same commands:

| Command | Description | Example |
| :--- | :--- | :--- |
| `/start` or `/help` | Displays help message and quick-start guide | `/help` |
| `/menu` | Open interactive button Control Center (Telegram) | `/menu` |
| `/add <url>` | Subscribe current chat/channel to an RSS feed | `/add https://example.com/rss/feed.xml` |
| `/list` | List all feeds monitored in this chat/channel | `/list` |
| `/get <id_or_url> [count]` | Inspect & preview live items from a feed immediately | `/get 1 5` or `/get https://... 3` |
| `/pause <id>` | Temporarily pause alerts for a feed (or `all`) | `/pause 1` or `/pause all` |
| `/resume <id>` | Resume alerts for a paused feed (or `all`) | `/resume 1` or `/resume all` |
| `/edit <id>` | Edit a feed in-place (title, filters, or URL) | `/edit 1 -title PSA -inf 1080p` |
| `/remove <id>` | Unsubscribe and stop monitoring a feed | `/remove 1` |
| `/filter <id> <rules>` | Configure keyword inclusion/exclusion rules | `/filter 1 inc=1080p,flac exc=cam,sample` |
| `/richtext [on/off]` | Toggle expandable quotes & rich cards (`/rt`) | `/richtext on` or `/rt toggle` |
| `/authorize <id>` | Authorize a user, chat, or forum topic (`/a`) | `/a -1001234567890` or `/a 12345` |
| `/unauthorize <id>` | Revoke authorization for a user or chat (`/ua`) | `/ua -1001234567890` |
| `/authlist` | View all authorized users, chats, and topics | `/authlist` |
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
# Telegram Bot Token (from @BotFather)
BOT_TOKEN = "123456789:ABCdefGhIJKlmNoPQRstuVWXyz"

# MongoDB Connection String (from MongoDB Atlas)
DATABASE_URL = "mongodb+srv://username:password@cluster.mongodb.net/?retryWrites=true&w=majority"

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
3. Copy the **HTTP API Token** provided by BotFather into `BOT_TOKEN` in your `config.py`.
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

## 🔍 Keyword Filtering & Advanced Boolean Logic

You can fine-tune what torrents and posts trigger alerts using rich boolean logic, compound exclusions, and case sensitivity:

- **Simple Inclusion / Exclusion:**
  ```text
  /filter 1 inc=1080p,flac,2160p
  /filter 1 exc=cam,telesync,hindi,sample
  /filter 1 inc=remux,bluray exc=cam,ts
  /filter 1 clear
  ```

- **Advanced Boolean Grouping (`|` for AND, `or` / `,` for OR):**
  Matches items that have **(1080p OR 720p) AND (PSA) AND (hevc OR x265)**:
  ```text
  /add https://example.com/rss/ -inf 1080p or 720p | PSA | hevc or x265
  ```

- **Compound Exclusion (`+` or `&`):**
  Only excludes the item if **both** words are present (e.g. drops items containing both `hindi` AND `dubbed`, while keeping English releases that just say `dubbed`):
  ```text
  /add https://example.com/rss/ -exf hindi + dubbed, cam, ts
  ```

- **Case-Sensitive Filtering (`-stv true` / `-cs true`):**
  Enforces strict casing (e.g. only matches uppercase `PSA` and not `psa` or `pSA`):
  ```text
  /add https://example.com/rss/ -inf PSA -stv true
  /edit 1 -stv false
  ```

- **File Size Filtering (`-maxsize` and `-minsize`):**
  Filter items by minimum and maximum file size (supports units: `B`, `KB`, `MB`, `GB`, `TB`):
  ```text
  /add https://example.com/rss/ -minsize 500MB -maxsize 5GB
  /edit 1 -maxsize 2.5GB
  /edit 1 -maxsize none
  ```

- **Batch Multi-Line Subscriptions:**
  Add multiple feeds in a single command (one feed per line) with custom titles, filters, and size constraints:
  ```text
  /add
  PSA Movies https://example.com/rss1 -inf 1080p
  4K HDR https://example.com/rss2 -minsize 5GB
  Anime https://example.com/rss3 -exf 720p
  ```

---

## ☁️ Cloud Deployment (Heroku, Render, Railway, Koyeb)

FeedPulse is pre-configured for seamless cloud deployment. You don't need to commit your `config.py`—simply set your tokens in your platform's dashboard **Environment Variables**:

| Variable | Description | Required |
| :--- | :--- | :--- |
| `BOT_TOKEN` | Telegram Bot Token from @BotFather | Yes (if using Telegram) |
| `DATABASE_URL` | MongoDB URI from MongoDB Atlas (`mongodb+srv://...`) | **Yes** |
| `DISCORD_BOT_TOKEN` | Discord Bot Token from Developer Portal | Optional |
| `POLL_INTERVAL_SECONDS` | Feed polling frequency in seconds (default: `120`) | Optional |

### 🟣 Deploy to Heroku
1. Create a new **app** on [Heroku](https://www.heroku.com).
2. Add `BOT_TOKEN` and `DATABASE_URL` under Settings → Config Vars.
3. In your Heroku app's **Resources** tab, enable the `worker` dyno.

### 🔵 Deploy to Render
1. Create a new **Background Worker** on [Render](https://render.com).
2. Connect your `FeedPulse` repository (Render auto-detects `render.yaml`).
3. Add `BOT_TOKEN` and `DATABASE_URL` under **Environment Variables**.

### 🚂 Deploy to Railway
1. Create a **New Project** on [Railway](https://railway.app) and select your GitHub repo.
2. Railway will automatically detect the `Procfile` and deploy as a worker service.
3. In the **Variables** tab, add `BOT_TOKEN` and `DATABASE_URL`.

### ⚡ Deploy to Koyeb
1. Create a new **service** on [Koyeb](https://app.koyeb.com) from GitHub.
2. Select your repository and choose **Worker** service type.
3. Add `BOT_TOKEN` and `DATABASE_URL` in the environment settings.

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

## 🎨 Custom Notification Templates & Rich Text

FeedPulse features customizable alert templates with modern Telegram rich text (expandable magnet blockquotes and styled metadata cards):

* **Global Setting (`config.py`)**: Set `ENABLE_RICH_TEXT = True` or customize `TORRENT_ALERT_TEMPLATE` and `FEED_ALERT_TEMPLATE`.
* **Per-Chat Control**: Admins can toggle rich text anytime using `/richtext on`, `/richtext off`, or the interactive **`[ 🎨 Rich Text ]`** button inside `/menu`.
* **Safe Fallback**: If a custom template contains broken HTML tags, FeedPulse automatically catches the error and safely falls back to the built-in template so notifications are never dropped.

Available template variables:
`{title}`, `{feed_title}`, `{size}`, `{category}`, `{seeds}`, `{leechers}`, `{magnet}`, `{torrent_url}`, `{link}`, `{published}`, `{resolution}`, `{codec}`, `{year}`, `{description}`, `{meta_card}`, `{magnet_block}`.

---

## 📂 Project Architecture

```text
FeedPulse/
├── config_sample.py      # Configuration template file
├── main.py               # Unified application runner & lifecycle manager
├── database/
│   ├── __init__.py
│   └── db.py             # Asynchronous MongoDB database interface (motor)
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
├── docker-compose.yml    # Docker Compose orchestration
└── requirements.txt      # Python dependencies
```

---

## 📄 License

MIT License. Free to use, modify, and distribute.
