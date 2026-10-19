# ==============================================================================
# REQUIRED CONFIG
# ==============================================================================
BOT_TOKEN = ""  # Telegram Bot token from @BotFather (e.g. "123456789:ABCdef...")
DATABASE_URL = ""  # MongoDB URI (e.g. "mongodb+srv://username:password@cluster.mongodb.net/?retryWrites=true&w=majority")

# ==============================================================================
# ACCESS CONTROL & SECURITY
# ==============================================================================
OWNER_ID = 0  # Telegram User ID of bot owner (e.g. 12345678, get via /id or @userinfobot). 0 = open to all.
AUTHORIZED_CHATS = []  # List of allowed Chat/Group/Channel IDs (e.g. [-1001234567890]). Empty = allow any chat with authorized users.
SUDO_USERS = []  # List of additional Telegram User IDs authorized to manage feeds (e.g. [123456, 789012])

# ==============================================================================
# OPTIONAL CONFIG
# ==============================================================================
DISCORD_BOT_TOKEN = ""  # Discord Bot token (optional if running Telegram-only)
POLL_INTERVAL_SECONDS = 120  # Feed check frequency in seconds (default: 2 min)
MAX_FEEDS_PER_USER = (
    25  # Maximum active feed subscriptions allowed per user (0 = unlimited)
)
INITIAL_ITEMS_TO_ALERT = (
    0  # How many existing items to alert on first add (0 = only new uploads)
)
MAX_ITEMS_PER_CHECK = 15  # Max items alerted per check (flood protection)
SMART_TITLE_DEDUPLICATION = False  # Prevent duplicate alerts for the same movie/episode across different release groups (default: False)
USER_AGENT = (
    "FeedPulse/1.0 (RSS Torrent Notifier; https://github.com/PainKiller3/FeedPulse)"
)

# ==============================================================================
# TELEGRAM MINI APP & WEB SERVER CONFIG
# ==============================================================================
ENABLE_WEBAPP = True  # Enable embedded Web App & REST API server
WEBAPP_PORT = 8080  # Port for the embedded Web App server (default: 8080 or PORT env)
WEBAPP_HOST = "0.0.0.0"  # Interface to bind to
WEBAPP_URL = ""  # Public HTTPS URL of the Web App (e.g. "https://feedpulse.yourdomain.com", Render URL, or Cloudflare Tunnel)
# Note: Telegram requires an HTTPS URL to launch Mini Apps inside the Telegram client.
# Once set, users can open the dashboard via /webapp or the Telegram Chat Menu button.
WEBAPP_SHORT_NAME = ""  # Optional: App short name if configured via BotFather /newapp (e.g. "dashboard")

# ==============================================================================
# NOTIFICATION TEMPLATES & RICH TEXT
# ==============================================================================
# Enable Telegram rich text (expandable blockquotes for long magnets, sleek metadata cards).
# Default: True. Can also be toggled per-chat via /richtext or in /menu.
ENABLE_RICH_TEXT = True

# Custom Telegram Notification Templates
# Available Variables:
#   {title}         - Clean item title (automatically HTML-escaped)
#   {feed_title}    - Name or title of the feed (automatically HTML-escaped)
#   {size}          - Formatted file size (e.g. "4.80 GB")
#   {category}      - Item category / genre
#   {seeds}         - Seeder count (or "?")
#   {leechers}      - Leecher count (or "?")
#   {magnet}        - Full magnet URI
#   {torrent_url}   - Direct link to .torrent file
#   {link}          - Web page / details URL
#   {published}     - Publication date string
#   {resolution}    - Extracted resolution (e.g. "1080p", "2160p")
#   {codec}         - Extracted video codec (e.g. "HEVC", "x264")
#   {year}          - Extracted release year (e.g. "2024")
#   {description}   - Feed summary or description text (HTML-escaped)
#   {meta_card}     - Formatted metadata card (Category, Size, Seeds/Leechers)
#   {magnet_block}  - Formatted magnet block (expandable in rich text mode)

# Default Torrent Alert Template (replicates built-in design):
TORRENT_ALERT_TEMPLATE = """⚡ <b>New Torrent Uploaded!</b>
📡 <i>{feed_title}</i>

🎬 <b>{title}</b>

{meta_card}
{magnet_block}"""

# Default RSS / Article Alert Template:
FEED_ALERT_TEMPLATE = """📰 <b>New Feed Post:</b>
📡 <i>{feed_title}</i>

📝 <b>{title}</b>

📅 <i>{published}</i>"""

# ==============================================================================
# DEFAULT TRACKERS (for Magnet link generation)
# ==============================================================================
DEFAULT_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://explodie.org:6969/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://tracker.moeking.me:6969/announce",
    "udp://ipv4.tracker.harry.lu:80/announce",
    "udp://valakas.rollo.cc:6969/announce",
]
