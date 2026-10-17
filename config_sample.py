# ==============================================================================
# REQUIRED CONFIG
# ==============================================================================
BOT_TOKEN = ""  # Telegram Bot token from @BotFather (e.g. "123456789:ABCdef...")
DATABASE_URL = ""  # MongoDB URI (e.g. "mongodb+srv://username:password@cluster.mongodb.net/?retryWrites=true&w=majority")

# ==============================================================================
# OPTIONAL CONFIG
# ==============================================================================
DISCORD_BOT_TOKEN = ""  # Discord Bot token (optional if running Telegram-only)
POLL_INTERVAL_SECONDS = 120  # Feed check frequency in seconds (default: 2 min)
INITIAL_ITEMS_TO_ALERT = (
    0  # How many existing items to alert on first add (0 = only new uploads)
)
MAX_ITEMS_PER_CHECK = 15  # Max items alerted per check (flood protection)
USER_AGENT = (
    "FeedPulse/1.0 (RSS Torrent Notifier; https://github.com/PainKiller3/FeedPulse)"
)

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
