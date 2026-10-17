import asyncio
import logging
import signal
import sys
from typing import List

try:
    from config import (
        TELEGRAM_BOT_TOKEN,
        DISCORD_BOT_TOKEN,
        DATABASE_PATH,
        POLL_INTERVAL_SECONDS,
    )
except ImportError:
    from config_sample import (
        TELEGRAM_BOT_TOKEN,
        DISCORD_BOT_TOKEN,
        DATABASE_PATH,
        POLL_INTERVAL_SECONDS,
    )
from database.db import Database
from services.dispatcher import NotificationDispatcher
from services.poller import FeedPoller
from bots.telegram_bot import TelegramBotService
from bots.discord_bot import DiscordBotService

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
logger = logging.getLogger("FeedPulse")


async def main() -> None:
    print("=" * 60)
    print(" ⚡ FeedPulse - Realtime RSS & BitTorrent Notifier Bot")
    print("=" * 60)

    # 1. Validate tokens
    has_telegram = bool(TELEGRAM_BOT_TOKEN)
    has_discord = bool(DISCORD_BOT_TOKEN)

    if not has_telegram and not has_discord:
        print("\n❌ ERROR: No bot tokens configured in config.py!")
        print("Please edit config.py and configure at least one of:")
        print("  • TELEGRAM_BOT_TOKEN  (from https://t.me/BotFather)")
        print(
            "  • DISCORD_BOT_TOKEN   (from https://discord.com/developers/applications)"
        )
        print("\nSee config_sample.py for a complete template.")
        print("=" * 60)
        sys.exit(1)

    # 2. Initialize Database
    db = Database(DATABASE_PATH)
    await db.init_db()

    # 3. Initialize Dispatcher & Background Poller
    dispatcher = NotificationDispatcher()
    poller = FeedPoller(db, dispatcher)

    # 4. Instantiate Bot Services
    telegram_service = None
    discord_service = None

    if has_telegram:
        logger.info("Initializing Telegram Bot Service...")
        telegram_service = TelegramBotService(
            TELEGRAM_BOT_TOKEN, db, poller, dispatcher
        )
    else:
        logger.info("Telegram Bot is disabled (TELEGRAM_BOT_TOKEN not provided).")

    if has_discord:
        logger.info("Initializing Discord Bot Service...")
        discord_service = DiscordBotService(DISCORD_BOT_TOKEN, db, poller, dispatcher)
    else:
        logger.info("Discord Bot is disabled (DISCORD_BOT_TOKEN not provided).")

    # 5. Start Background Poller
    await poller.start()

    # 6. Prepare bot runner tasks
    tasks: List[asyncio.Task] = []

    if telegram_service:
        tasks.append(asyncio.create_task(telegram_service.start()))

    if discord_service:
        tasks.append(asyncio.create_task(discord_service.start()))

    # Graceful shutdown handler
    stop_event = asyncio.Event()

    def handle_signal():
        logger.info("Shutdown signal received...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_signal)
        except NotImplementedError:
            # Signal handling on Windows
            pass

    logger.info(
        "FeedPulse is running! (Checking feeds every %ds)", POLL_INTERVAL_SECONDS
    )

    try:
        # Run until interrupted
        await asyncio.gather(*tasks, return_exceptions=True)
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
