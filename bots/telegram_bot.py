import html
import logging
from typing import Optional, List, Set, Tuple
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
        owner_id: int = 0,
        authorized_chats: Optional[List[int]] = None,
        sudo_users: Optional[List[int]] = None,
    ):
        self.token = token
        self.db = db
        self.poller = poller
        self.dispatcher = dispatcher
        self.owner_id: int = owner_id or 0
        self.authorized_chats: Set[int] = set(authorized_chats or [])
        self.sudo_users: Set[int] = set(sudo_users or [])
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
        self.application.add_handler(CommandHandler("id", self.cmd_id))
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

    # ---------------- Authorization & Destination Helpers ----------------

    async def check_authorization(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> bool:
        """
        Verify if the calling user and chat are permitted to manage feeds.
        Returns True if authorized, False otherwise.
        """
        # If no access controls are set (OWNER_ID=0, no SUDO, no AUTH_CHATS), open mode
        if not self.owner_id and not self.sudo_users and not self.authorized_chats:
            return True

        user = update.effective_user
        chat = update.effective_chat
        user_id = user.id if user else 0
        chat_id = chat.id if chat else 0

        # Owner and Sudo users have global superuser access
        if (self.owner_id and user_id == self.owner_id) or (user_id in self.sudo_users):
            return True

        # In private DM: only owner and sudo users are allowed
        if chat and chat.type == "private":
            await update.effective_message.reply_html(
                f"⛔ <b>Access Denied:</b> You are not authorized to control this bot.\n\n"
                f"👤 <b>Your User ID:</b> <code>{user_id}</code>\n"
                "Please configure this ID in <code>OWNER_ID</code> or <code>SUDO_USERS</code>."
            )
            return False

        # In groups/channels: verify chat authorization if whitelist is configured
        if self.authorized_chats and chat_id not in self.authorized_chats:
            logger.warning(
                "Unauthorized command attempt in chat %s (%s) by user %s",
                chat_id,
                chat.title if chat else "",
                user_id,
            )
            return False

        # If in an authorized group, check if user is group Administrator or Creator
        if chat and chat.type in ("group", "supergroup"):
            try:
                member = await chat.get_member(user_id)
                if member.status in ("creator", "administrator"):
                    return True
            except Exception as e:
                logger.debug("Failed to verify group admin status: %s", e)

        await update.effective_message.reply_html(
            "⛔ <b>Permission Denied:</b> Only the Bot Owner, Sudo Users, or Group Admins can manage feeds."
        )
        return False

    async def _resolve_destination(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        target_arg: Optional[str] = None,
        thread_arg: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[int], Optional[str]]:
        """
        Resolve target chat/channel ID, thread ID, and display title.
        Returns (target_id, thread_id, target_title).
        """
        if target_arg:
            target_str = target_arg.strip()
            try:
                target_chat = await context.bot.get_chat(target_str)
            except Exception as e:
                await update.effective_message.reply_html(
                    f"❌ Cannot access destination <code>{html.escape(target_str)}</code>: {html.escape(str(e))}\n\n"
                    "Make sure the bot has been added to that chat/channel!"
                )
                return None, None, None

            # For channels, verify bot is admin with posting permission
            if target_chat.type == "channel":
                try:
                    bot_member = await context.bot.get_chat_member(
                        target_chat.id, context.bot.id
                    )
                    if bot_member.status != "administrator" or not getattr(
                        bot_member, "can_post_messages", True
                    ):
                        await update.effective_message.reply_html(
                            f"⚠️ The bot must be an <b>Administrator</b> in "
                            f"<b>{html.escape(target_chat.title or target_str)}</b> with <b>Post Messages</b> permission!"
                        )
                        return None, None, None
                except Exception as e:
                    logger.warning(
                        "Could not verify bot admin status in channel: %s", e
                    )

            thread_id = (
                int(thread_arg)
                if thread_arg and thread_arg.lstrip("-").isdigit()
                else None
            )
            title = target_chat.title or (
                f"@{target_chat.username}"
                if target_chat.username
                else str(target_chat.id)
            )
            return str(target_chat.id), thread_id, title

        # Default: current chat
        chat = update.effective_chat
        target_id = str(chat.id)
        target_title = chat.title or (
            f"@{chat.username}" if chat.username else "Private Chat"
        )

        # Check if in a topic/forum
        thread_id = None
        msg = update.effective_message
        if msg and getattr(msg, "is_topic_message", False) and msg.message_thread_id:
            thread_id = msg.message_thread_id

        return target_id, thread_id, target_title

    # ---------------- Command Handlers ----------------

    async def cmd_help(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show welcome message and bot commands."""
        text = (
            "⚡ <b>Welcome to FeedPulse!</b>\n\n"
            "Real-time RSS & BitTorrent notifications with file sizes, seeds, "
            "direct <code>.torrent</code> downloads, and 1-click Magnet links!\n\n"
            "<b>Available Commands:</b>\n"
            "➕ <code>/add &lt;url&gt; [chat/channel] [topic_id]</code> - Subscribe to a feed\n"
            "📋 <code>/list [chat/channel]</code> - View subscribed feeds\n"
            "➖ <code>/remove &lt;id&gt; [chat/channel]</code> - Unsubscribe from a feed\n"
            "🔍 <code>/filter &lt;id&gt; inc=&lt;words&gt; exc=&lt;words&gt;</code> - Keyword filters\n"
            "🔄 <code>/check</code> - Force an immediate feed check\n"
            "🧪 <code>/test [chat/channel]</code> - Preview sample torrent notification\n"
            "🆔 <code>/id</code> - Display Chat ID, Topic ID, and your User ID\n\n"
            "<b>Examples:</b>\n"
            "• Subscribe current chat / topic:\n"
            "  <code>/add https://example.com/rss/feed.xml</code>\n"
            "• Subscribe a channel from DM:\n"
            "  <code>/add https://example.com/rss/feed.xml @MyChannel</code>\n"
            "• Subscribe a forum topic from DM:\n"
            "  <code>/add https://example.com/rss/feed.xml -1001234567890 42</code>"
        )
        await update.effective_message.reply_html(text, disable_web_page_preview=True)

    async def cmd_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Display Telegram User ID, Chat ID, and Topic / Thread ID."""
        user = update.effective_user
        chat = update.effective_chat
        msg = update.effective_message

        lines = [
            "🆔 <b>Telegram Identification:</b>\n",
            f"👤 <b>Your User ID:</b> <code>{user.id}</code> ({html.escape(user.full_name or '')})",
            f"💬 <b>Chat ID:</b> <code>{chat.id}</code> ({html.escape(chat.title or chat.type)})",
        ]
        if chat.username:
            lines.append(f"🔗 <b>Username:</b> @{chat.username}")
        if msg and getattr(msg, "is_topic_message", False) and msg.message_thread_id:
            lines.append(
                f"📌 <b>Topic / Thread ID:</b> <code>{msg.message_thread_id}</code>"
            )

        await update.effective_message.reply_html("\n".join(lines))

    async def cmd_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Subscribe a chat, channel, or topic to an RSS feed."""
        if not await self.check_authorization(update, context):
            return

        if not context.args:
            await update.effective_message.reply_html(
                "⚠️ <b>Usage:</b>\n"
                "• <code>/add &lt;feed_url&gt;</code> (current chat/topic)\n"
                "• <code>/add &lt;feed_url&gt; &lt;@channel_or_chat_id&gt; [topic_id]</code>\n\n"
                "<b>Example:</b>\n"
                "<code>/add https://example.com/rss/feed.xml @MyChannel</code>"
            )
            return

        url = context.args[0].strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            await update.effective_message.reply_html(
                "❌ Please provide a valid HTTP/HTTPS URL."
            )
            return

        target_arg = context.args[1] if len(context.args) > 1 else None
        thread_arg = context.args[2] if len(context.args) > 2 else None

        target_id, thread_id, target_title = await self._resolve_destination(
            update, context, target_arg, thread_arg
        )
        if not target_id:
            return

        status_msg = await update.effective_message.reply_html(
            "⏳ <i>Validating and inspecting RSS feed...</i>"
        )

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
                platform="telegram",
                target_id=target_id,
                url=url,
                title=feed_title,
                thread_id=thread_id,
            )

            # Signal poller to register and track
            self.poller.trigger_immediate_check()

            dest_str = f"<b>{html.escape(target_title)}</b> (<code>{target_id}</code>)"
            if thread_id:
                dest_str += f" [Topic: <code>#{thread_id}</code>]"

            if is_new:
                reply_text = (
                    f"✅ <b>Successfully Subscribed!</b>\n\n"
                    f"📡 <b>Feed:</b> {html.escape(effective_title)}\n"
                    f"🆔 <b>Feed ID:</b> <code>{feed_id}</code>\n"
                    f"🎯 <b>Destination:</b> {dest_str}\n"
                    f"🔗 <b>URL:</b> {html.escape(url)}\n"
                    f"📦 <b>Current Items:</b> {len(items)}\n\n"
                    f"🔔 FeedPulse will notify every {POLL_INTERVAL_SECONDS}s when new uploads appear."
                )
            else:
                reply_text = (
                    f"ℹ️ <b>Already Subscribed!</b>\n\n"
                    f"{dest_str} is already monitoring <b>{html.escape(effective_title)}</b> (ID: <code>{feed_id}</code>)."
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
        """List feeds subscribed in this chat, topic, or target channel."""
        if not await self.check_authorization(update, context):
            return

        target_arg = context.args[0] if context.args else None
        target_id, thread_id, target_title = await self._resolve_destination(
            update, context, target_arg
        )
        if not target_id:
            return

        subs = await self.db.list_subscriptions(
            "telegram", target_id, thread_id=thread_id
        )

        dest_label = html.escape(target_title)
        if thread_id:
            dest_label += f" [Topic: #{thread_id}]"

        if not subs:
            await update.effective_message.reply_html(
                f"📭 <b>No active subscriptions for {dest_label}.</b>\n\n"
                "Use <code>/add &lt;feed_url&gt;</code> to start monitoring a feed!"
            )
            return

        lines = [f"📋 <b>Active Subscriptions for {dest_label} ({len(subs)}):</b>\n"]
        for s in subs:
            title = html.escape(s.get("title") or "Untitled Feed")
            url = html.escape(s.get("url", ""))
            fid = s["feed_id"]
            checked = s.get("last_checked") or "Not yet checked"
            tid = s.get("thread_id")

            topic_badge = f" [Topic: <code>#{tid}</code>]" if tid else ""
            lines.append(f"• <b>[{fid}] {title}</b>{topic_badge}")
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
        if not await self.check_authorization(update, context):
            return

        if not context.args:
            await update.effective_message.reply_html(
                "⚠️ <b>Usage:</b>\n"
                "• <code>/remove &lt;feed_id_or_url&gt;</code> (current chat/topic)\n"
                "• <code>/remove &lt;feed_id_or_url&gt; &lt;@channel_or_chat_id&gt;</code>\n\n"
                "Check your feeds with <code>/list</code> first."
            )
            return

        feed_identifier = context.args[0].strip()
        target_arg = context.args[1] if len(context.args) > 1 else None

        target_id, thread_id, target_title = await self._resolve_destination(
            update, context, target_arg
        )
        if not target_id:
            return

        removed_name = await self.db.unsubscribe(
            "telegram", target_id, feed_identifier, thread_id=thread_id
        )

        dest_str = html.escape(target_title)
        if thread_id:
            dest_str += f" [Topic: #{thread_id}]"

        if removed_name:
            await update.effective_message.reply_html(
                f"🗑 <b>Removed subscription:</b>\n"
                f"<b>{html.escape(removed_name)}</b> has been unsubscribed from {dest_str}."
            )
        else:
            await update.effective_message.reply_html(
                f"❌ Subscription not found for <code>{html.escape(feed_identifier)}</code> in {dest_str}.\n"
                "Use <code>/list</code> to see your active feed IDs."
            )

    async def cmd_filter(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """
        Configure keyword filters for a feed subscription:
        /filter <feed_id> inc=1080p,flac exc=cam,hindi [@channel]
        """
        if not await self.check_authorization(update, context):
            return

        if not context.args or len(context.args) < 2:
            await update.effective_message.reply_html(
                "⚠️ <b>Usage:</b>\n"
                "<code>/filter &lt;feed_id&gt; inc=&lt;words&gt; exc=&lt;words&gt; [@channel]</code>\n\n"
                "<b>Example:</b>\n"
                "<code>/filter 1 inc=1080p,flac exc=cam,hindi</code>\n"
                "<i>To clear filters:</i> <code>/filter 1 clear</code>"
            )
            return

        feed_id_str = context.args[0].strip()
        if not feed_id_str.isdigit():
            await update.effective_message.reply_html(
                "❌ Please provide a numeric feed ID (e.g. <code>/filter 1 ...</code>)."
            )
            return

        # Check if last argument is an explicit target channel/chat
        target_arg = None
        filter_parts = context.args[1:]
        if filter_parts and (
            filter_parts[-1].startswith("@")
            or (filter_parts[-1].startswith("-100") and filter_parts[-1][1:].isdigit())
        ):
            target_arg = filter_parts[-1]
            filter_parts = filter_parts[:-1]

        target_id, thread_id, target_title = await self._resolve_destination(
            update, context, target_arg
        )
        if not target_id:
            return

        params = " ".join(filter_parts).strip()
        inc_val = None
        exc_val = None

        if "clear" in params.lower():
            inc_val = ""
            exc_val = ""
        else:
            for part in filter_parts:
                if part.lower().startswith("inc="):
                    inc_val = part[4:].strip()
                elif part.lower().startswith("exc="):
                    exc_val = part[4:].strip()

        success = await self.db.update_filters(
            platform="telegram",
            target_id=target_id,
            feed_id=int(feed_id_str),
            filter_keywords=inc_val,
            filter_exclude=exc_val,
            thread_id=thread_id,
        )

        dest_str = html.escape(target_title)
        if thread_id:
            dest_str += f" [Topic: #{thread_id}]"

        if success:
            await update.effective_message.reply_html(
                f"✅ <b>Filter updated for Feed #{feed_id_str} ({dest_str})!</b>\n"
                f"• Include: <code>{html.escape(inc_val or 'Any')}</code>\n"
                f"• Exclude: <code>{html.escape(exc_val or 'None')}</code>"
            )
        else:
            await update.effective_message.reply_html(
                f"❌ Could not find an active subscription for Feed #{feed_id_str} in {dest_str}."
            )

    async def cmd_check(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Trigger an immediate feed check cycle."""
        if not await self.check_authorization(update, context):
            return

        await update.effective_message.reply_html(
            "🔄 <i>Triggering immediate feed check cycle...</i>"
        )
        self.poller.trigger_immediate_check()

    async def cmd_test(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Send a test torrent alert to verify notifications and magnet links."""
        if not await self.check_authorization(update, context):
            return

        target_arg = context.args[0] if context.args else None
        target_id, thread_id, target_title = await self._resolve_destination(
            update, context, target_arg
        )
        if not target_id:
            return

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
        await self.dispatcher.send_telegram(
            target_id, sample_item, "Sample RSS Feed (Preview)", thread_id=thread_id
        )

        if target_id != str(update.effective_chat.id):
            await update.effective_message.reply_html(
                f"✅ Test alert sent to <b>{html.escape(target_title)}</b>!"
            )
