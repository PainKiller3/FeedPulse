import asyncio
import logging
import signal
import sys

import os
from database.db import Database
from services.dispatcher import NotificationDispatcher
from services.poller import FeedPoller
from bots.telegram_bot import TelegramBotService
from bots.discord_bot import DiscordBotService

try:
    import config
except ImportError:
    import config_sample as config

# Allow cloud platforms (Heroku, Render, Railway, Koyeb) to provide env vars directly
BOT_TOKEN = os.environ.get("BOT_TOKEN") or getattr(config, "BOT_TOKEN", "").strip()
DATABASE_URL = (
    os.environ.get("DATABASE_URL") or getattr(config, "DATABASE_URL", "").strip()
)
DISCORD_BOT_TOKEN = (
    os.environ.get("DISCORD_BOT_TOKEN")
    or getattr(config, "DISCORD_BOT_TOKEN", "").strip()
)
POLL_INTERVAL_SECONDS = int(
    os.environ.get("POLL_INTERVAL_SECONDS")
    or getattr(config, "POLL_INTERVAL_SECONDS", 120)
)


def _parse_id_list(val) -> list[int]:
    """Parse integer IDs from lists or comma/space-separated strings."""
    if isinstance(val, (list, tuple, set)):
        return [int(x) for x in val if str(x).lstrip("-").isdigit()]
    if isinstance(val, str) and val.strip():
        parts = [p.strip() for p in val.replace(",", " ").split()]
        return [int(p) for p in parts if p.lstrip("-").isdigit()]
    return []


OWNER_ID = int(os.environ.get("OWNER_ID") or getattr(config, "OWNER_ID", 0) or 0)
AUTHORIZED_CHATS = _parse_id_list(
    os.environ.get("AUTHORIZED_CHATS") or getattr(config, "AUTHORIZED_CHATS", [])
)
SUDO_USERS = _parse_id_list(
    os.environ.get("SUDO_USERS")
    or os.environ.get("AUTHORIZED_USERS")
    or getattr(config, "SUDO_USERS", [])
    or getattr(config, "AUTHORIZED_USERS", [])
)
MAX_FEEDS_PER_USER = int(
    os.environ.get("MAX_FEEDS_PER_USER")
    or getattr(config, "MAX_FEEDS_PER_USER", 25)
    or 25
)
SMART_TITLE_DEDUPLICATION = (
    os.environ.get("SMART_TITLE_DEDUPLICATION", "").lower() in ("true", "1", "yes")
    if "SMART_TITLE_DEDUPLICATION" in os.environ
    else bool(getattr(config, "SMART_TITLE_DEDUPLICATION", False))
)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# Silence verbose HTTP request logging from Telegram long-polling
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("FeedPulse")


async def main() -> None:
    print("=" * 60)
    print(" ⚡ FeedPulse - Realtime RSS & BitTorrent Notifier Bot")
    print("=" * 60)

    # 1. Validate tokens & database
    has_telegram = bool(BOT_TOKEN)
    has_discord = bool(DISCORD_BOT_TOKEN)

    if not has_telegram and not has_discord:
        print("\n❌ ERROR: No bot tokens configured in config.py!")
        print("Please edit config.py and configure at least one of:")
        print("  • BOT_TOKEN           (from https://t.me/BotFather)")
        print(
            "  • DISCORD_BOT_TOKEN   (from https://discord.com/developers/applications)"
        )
        print("\nSee config_sample.py for a complete template.")
        print("=" * 60)
        sys.exit(1)

    if not DATABASE_URL:
        print("\n❌ ERROR: DATABASE_URL is missing in config.py!")
        print("Please configure your MongoDB connection string in config.py:")
        print(
            '  • DATABASE_URL = "mongodb+srv://..." (Free from https://www.mongodb.com/atlas)'
        )
        print("\nSee config_sample.py for a complete template.")
        print("=" * 60)
        sys.exit(1)

    # 2. Initialize Database
    db = Database(DATABASE_URL)
    try:
        await db.init_db()
    except Exception as e:
        logger.error("Failed to connect to MongoDB: %s", e)
        print(f"\n❌ ERROR: Could not connect to MongoDB: {e}")
        print(
            "Please check your DATABASE_URL in config.py and ensure IP access is configured in MongoDB Atlas."
        )
        print("=" * 60)
        sys.exit(1)

    # 3. Initialize Dispatcher & Background Poller
    dispatcher = NotificationDispatcher()
    poller = FeedPoller(
        db, dispatcher, smart_title_deduplication=SMART_TITLE_DEDUPLICATION
    )
    logger.info(
        "Deduplication: Smart Cross-Group Title Deduplication is %s",
        "ENABLED" if SMART_TITLE_DEDUPLICATION else "DISABLED",
    )

    # 4. Instantiate Bot Services
    telegram_service = None
    discord_service = None

    if has_telegram:
        logger.info("Initializing Telegram Bot Service...")
        if OWNER_ID:
            logger.info("Security: Bot Owner ID set to %s", OWNER_ID)
        if SUDO_USERS:
            logger.info(
                "Security: %d Sudo/Authorized User(s) configured", len(SUDO_USERS)
            )
        if AUTHORIZED_CHATS:
            logger.info(
                "Security: %d Authorized Chat(s) configured", len(AUTHORIZED_CHATS)
            )
        telegram_service = TelegramBotService(
            BOT_TOKEN,
            db,
            poller,
            dispatcher,
            owner_id=OWNER_ID,
            authorized_chats=AUTHORIZED_CHATS,
            sudo_users=SUDO_USERS,
            max_feeds_per_user=MAX_FEEDS_PER_USER,
        )
    else:
        logger.info("Telegram Bot is disabled (BOT_TOKEN not provided).")

    if has_discord:
        logger.info("Initializing Discord Bot Service...")
        discord_service = DiscordBotService(DISCORD_BOT_TOKEN, db, poller, dispatcher)
    else:
        logger.info("Discord Bot is disabled (DISCORD_BOT_TOKEN not provided).")

    # 5. Start Background Poller
    await poller.start()

    # 6. Start bot services
    discord_task = None
    if telegram_service:
        await telegram_service.start()

    if discord_service:
        discord_task = asyncio.create_task(discord_service.start())

    # Graceful shutdown handler
    stop_event = asyncio.Event()

    def handle_signal():
        logger.info("Shutdown signal received...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_signal)
        except (NotImplementedError, AttributeError):
            # Signal handling on Windows
            pass

    logger.info(
        "FeedPulse is running! (Checking feeds every %ds)", POLL_INTERVAL_SECONDS
    )

    try:
        if discord_task:
            stop_task = asyncio.create_task(stop_event.wait())
            done, pending = await asyncio.wait(
                [discord_task, stop_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for p in pending:
                p.cancel()
        else:
            await stop_event.wait()
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        logger.info("Shutting down FeedPulse gracefully...")
        await poller.stop()
        if telegram_service:
            await telegram_service.stop()
        if discord_service:
            await discord_service.stop()
        logger.info("FeedPulse terminated safely. Goodbye!")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nProcess interrupted by user. Exiting...")
