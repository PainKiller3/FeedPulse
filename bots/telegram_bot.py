import html
import logging
from typing import Optional
import aiohttp
from telegram import Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes

from database.db import Database
from parser.feed_parser import FeedParser, FeedItem
from services.dispatcher import NotificationDispatcher
from services.poller import FeedPoller

try:
    from config import POLL_INTERVAL_SECONDS
except ImportError:
    from config_sample import POLL_INTERVAL_SECONDS

logger = logging.getLogger(__name__)


class TelegramBotService:
    """Telegram bot interface for FeedPulse."""

    def __init__(
        self,
        token: str,
        db: Database,
        poller: FeedPoller,
        dispatcher: NotificationDispatcher,
    ):
        self.token = token
        self.db = db
        self.poller = poller
        self.dispatcher = dispatcher
        self.application: Optional[Application] = None

    async def start(self) -> None:
        """Initialize and start the Telegram bot polling."""
        if not self.token:
            logger.warning("Telegram bot token not provided; Telegram bot is disabled.")
            return

        self.application = ApplicationBuilder().token(self.token).build()

        # Connect bot instance to dispatcher so poller can send messages
        self.dispatcher.set_telegram_bot(self.application.bot)

        # Register command handlers
        self.application.add_handler(CommandHandler(["start", "help"], self.cmd_help))
        self.application.add_handler(CommandHandler("add", self.cmd_add))
        self.application.add_handler(CommandHandler(["list", "feeds"], self.cmd_list))
        self.application.add_handler(
            CommandHandler(["remove", "del", "delete"], self.cmd_remove)
        )
        self.application.add_handler(CommandHandler("filter", self.cmd_filter))
        self.application.add_handler(CommandHandler("check", self.cmd_check))
        self.application.add_handler(CommandHandler("test", self.cmd_test))

        await self.application.initialize()
        await self.application.start()
        await self.application.updater.start_polling(drop_pending_updates=True)
        bot_info = await self.application.bot.get_me()
        logger.info(
            "Telegram Bot started successfully as @%s (ID: %s)",
            bot_info.username,
            bot_info.id,
        )

    async def stop(self) -> None:
        """Gracefully stop the Telegram bot."""
        if self.application:
            logger.info("Stopping Telegram Bot...")
            if self.application.updater and self.application.updater.is_running:
                await self.application.updater.stop()
            if self.application.is_running:
                await self.application.stop()
            await self.application.shutdown()
            logger.info("Telegram Bot stopped")

    # ---------------- Command Handlers ----------------

    async def cmd_help(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show welcome message and bot commands."""
        text = (
            "⚡ <b>Welcome to FeedPulse!</b>\n\n"
            "I monitor RSS & BitTorrent feeds in real-time and alert you immediately "
            "when new torrents or articles are uploaded, complete with file sizes, "
            "seeds, direct <code>.torrent</code> downloads, and 1-click Magnet links!\n\n"
            "<b>Available Commands:</b>\n"
            "➕ <code>/add &lt;feed_url&gt;</code> - Subscribe to an RSS feed\n"
            "📋 <code>/list</code> - View all subscribed feeds in this chat\n"
            "➖ <code>/remove &lt;id_or_url&gt;</code> - Unsubscribe from a feed\n"
            "🔍 <code>/filter &lt;id&gt; inc=&lt;words&gt; exc=&lt;words&gt;</code> - Set keyword filters\n"
            "🔄 <code>/check</code> - Force an immediate feed check\n"
            "🧪 <code>/test</code> - Preview a sample torrent notification\n\n"
            "<b>Example:</b>\n"
            "<code>/add https://example.com/rss/feed.xml</code>"
        )
        await update.effective_message.reply_html(text, disable_web_page_preview=True)

    async def cmd_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Subscribe this chat to an RSS feed."""
        chat_id = str(update.effective_chat.id)

        if not context.args:
            await update.effective_message.reply_html(
                "⚠️ <b>Usage:</b> <code>/add &lt;feed_url&gt;</code>\n\n"
                "<b>Example:</b>\n"
                "<code>/add https://example.com/rss/feed.xml</code>"
            )
            return

        url = context.args[0].strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            await update.effective_message.reply_html(
                "❌ Please provide a valid HTTP/HTTPS URL."
            )
            return

        status_msg = await update.effective_message.reply_html(
            "⏳ <i>Validating and inspecting RSS feed...</i>"
        )

        # Test fetching the feed
        try:
            async with aiohttp.ClientSession() as session:
                title, etag, modified, items, status = await FeedParser.fetch_and_parse(
                    session, url
                )

            if status != 200 or not items:
                await status_msg.edit_text(
                    f"❌ Could not load feed from {html.escape(url)} (HTTP Status: {status}).\n"
                    "Please check if the URL is accessible and returns valid RSS/XML."
                )
                return

            feed_title = title or url
            feed_id, effective_title, is_new = await self.db.subscribe(
                platform="telegram", target_id=chat_id, url=url, title=feed_title
            )

            # Signal poller to register and track
            self.poller.trigger_immediate_check()

            if is_new:
                reply_text = (
                    f"✅ <b>Successfully Subscribed!</b>\n\n"
                    f"📡 <b>Feed:</b> {html.escape(effective_title)}\n"
                    f"🆔 <b>Feed ID:</b> <code>{feed_id}</code>\n"
                    f"🔗 <b>URL:</b> {html.escape(url)}\n"
                    f"📦 <b>Current Items:</b> {len(items)}\n\n"
                    f"🔔 FeedPulse will now notify this chat every {POLL_INTERVAL_SECONDS}s when new uploads appear."
                )
            else:
                reply_text = (
                    f"ℹ️ <b>Already Subscribed!</b>\n\n"
                    f"This chat is already monitoring <b>{html.escape(effective_title)}</b> (ID: <code>{feed_id}</code>)."
                )

            await status_msg.edit_text(
                reply_text, parse_mode="HTML", disable_web_page_preview=True
            )

        except Exception as e:
            logger.error("Error in /add command: %s", e)
            await status_msg.edit_text(
                f"❌ Error while adding feed: {html.escape(str(e))}"
            )

    async def cmd_list(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List feeds subscribed in this chat."""
        chat_id = str(update.effective_chat.id)
        subs = await self.db.list_subscriptions("telegram", chat_id)

        if not subs:
            await update.effective_message.reply_html(
                "📭 <b>No active subscriptions in this chat.</b>\n\n"
                "Use <code>/add &lt;feed_url&gt;</code> to start monitoring a feed!"
            )
            return

        lines = [f"📋 <b>Active Feed Subscriptions ({len(subs)}):</b>\n"]
        for s in subs:
            title = html.escape(s["title"] or "Untitled Feed")
            url = html.escape(s["url"])
            fid = s["feed_id"]
            checked = s["last_checked"] or "Not yet checked"

            lines.append(f"• <b>[{fid}] {title}</b>")
            lines.append(f"  🔗 <code>{url}</code>")
            if s.get("filter_keywords"):
                lines.append(
                    f"  🔍 <i>Include: {html.escape(s['filter_keywords'])}</i>"
                )
            if s.get("filter_exclude"):
                lines.append(f"  🚫 <i>Exclude: {html.escape(s['filter_exclude'])}</i>")
            lines.append(f"  ⏱ <i>Last Check: {checked}</i>\n")

        lines.append("<i>To remove a feed, use:</i> <code>/remove &lt;id&gt;</code>")
        await update.effective_message.reply_html(
            "\n".join(lines), disable_web_page_preview=True
        )

    async def cmd_remove(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Unsubscribe from a feed."""
        chat_id = str(update.effective_chat.id)

        if not context.args:
            await update.effective_message.reply_html(
                "⚠️ <b>Usage:</b> <code>/remove &lt;feed_id_or_url&gt;</code>\n"
                "Check your feeds with <code>/list</code> first."
            )
            return

        feed_identifier = context.args[0].strip()
        removed_name = await self.db.unsubscribe("telegram", chat_id, feed_identifier)

        if removed_name:
            await update.effective_message.reply_html(
                f"🗑 <b>Removed subscription:</b>\n"
                f"<b>{html.escape(removed_name)}</b> has been unsubscribed from this chat."
            )
        else:
            await update.effective_message.reply_html(
                f"❌ Subscription not found for <code>{html.escape(feed_identifier)}</code>.\n"
                "Use <code>/list</code> to see your active feed IDs."
            )

    async def cmd_filter(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """
        Configure keyword filters for a feed subscription:
        /filter <feed_id> inc=1080p,flac exc=cam,hindi
        """
        chat_id = str(update.effective_chat.id)

        if not context.args or len(context.args) < 2:
            await update.effective_message.reply_html(
                "⚠️ <b>Usage:</b>\n"
                "<code>/filter &lt;feed_id&gt; inc=&lt;words&gt; exc=&lt;words&gt;</code>\n\n"
                "<b>Example:</b>\n"
                "<code>/filter 1 inc=1080p,flac exc=cam,hindi</code>\n"
                "<i>To clear filters:</i> <code>/filter 1 clear</code>"
            )
            return

        feed_id_str = context.args[0].strip()
        params = " ".join(context.args[1:]).strip()

        inc_val = None
        exc_val = None

        if "clear" in params.lower():
            inc_val = ""
            exc_val = ""
        else:
            for part in context.args[1:]:
                if part.lower().startswith("inc="):
                    inc_val = part[4:].strip()
                elif part.lower().startswith("exc="):
                    exc_val = part[4:].strip()

        if not feed_id_str.isdigit():
            await update.effective_message.reply_html(
                "❌ Please provide a numeric feed ID (e.g. <code>/filter 1 ...</code>)."
            )
            return

        success = await self.db.update_filters(
            platform="telegram",
            target_id=chat_id,
            feed_id=int(feed_id_str),
            filter_keywords=inc_val,
            filter_exclude=exc_val,
        )

        if success:
            await update.effective_message.reply_html(
                f"✅ <b>Filter updated for Feed #{feed_id_str}!</b>\n"
                f"• Include: <code>{html.escape(inc_val or 'Any')}</code>\n"
                f"• Exclude: <code>{html.escape(exc_val or 'None')}</code>"
            )
        else:
            await update.effective_message.reply_html(
                f"❌ Could not find an active subscription for Feed #{feed_id_str} in this chat."
            )

    async def cmd_check(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Trigger an immediate feed check cycle."""
        await update.effective_message.reply_html(
            "🔄 <i>Triggering immediate feed check cycle...</i>"
        )
        self.poller.trigger_immediate_check()

    async def cmd_test(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Send a test torrent alert to verify notifications and magnet links."""
        sample_item = FeedItem(
            title="Ubuntu 24.04 LTS Desktop (64-bit)",
            link="https://example.com/item/101",
            guid="test-sample-guid-101",
            item_key="ih:2CB48F2892F15B77866F7776BD34219B0C499C1D",
            category="Linux / OS",
            size_str="4.80 GB",
            size_bytes=5153960755,
            seeds=125,
            leechers=30,
            torrent_url="https://example.com/downloads/ubuntu.torrent",
            info_hash="2CB48F2892F15B77866F7776BD34219B0C499C1D",
            magnet_uri=FeedParser.build_magnet_uri(
                "2CB48F2892F15B77866F7776BD34219B0C499C1D",
                "Ubuntu 24.04 LTS Desktop (64-bit)",
            ),
        )
        text, reply_markup = self.dispatcher.format_telegram_message(
            sample_item, "Sample RSS Feed (Preview)"
        )
        await update.effective_message.reply_html(
            text, reply_markup=reply_markup, disable_web_page_preview=False
        )
