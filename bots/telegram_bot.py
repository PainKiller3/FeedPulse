import html
import logging
from typing import Optional, List, Set, Tuple
import aiohttp
from telegram import BotCommand, Update
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
        max_feeds_per_user: int = 25,
    ):
        self.token = token
        self.db = db
        self.poller = poller
        self.dispatcher = dispatcher
        self.owner_id: int = owner_id or 0
        self.authorized_chats: Set[int] = set(authorized_chats or [])
        self.sudo_users: Set[int] = set(sudo_users or [])
        self.max_feeds_per_user: int = max_feeds_per_user
        self.application: Optional[Application] = None

    def is_super_user(self, user_id: int) -> bool:
        """Check if a user has bot owner or sudo privileges."""
        if not user_id:
            return False
        return (self.owner_id and user_id == self.owner_id) or (
            user_id in self.sudo_users
        )

    async def is_admin_or_super(self, update: Update) -> bool:
        """Check if caller has bot superuser or group admin privileges."""
        user = update.effective_user
        chat = update.effective_chat
        if not user:
            return False
        if self.is_super_user(user.id):
            return True
        if chat and chat.type in ("group", "supergroup"):
            try:
                member = await chat.get_member(user.id)
                if member.status in ("creator", "administrator"):
                    return True
            except Exception:
                pass
        return False

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
            CommandHandler(["myfeeds", "mysubs"], self.cmd_myfeeds)
        )
        self.application.add_handler(
            CommandHandler(["remove", "del", "delete"], self.cmd_remove)
        )
        self.application.add_handler(CommandHandler(["pause", "mute"], self.cmd_pause))
        self.application.add_handler(
            CommandHandler(["resume", "unmute"], self.cmd_resume)
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

        # Automatically register command autocompletion menu with Telegram
        commands = [
            BotCommand("add", "Subscribe to an RSS/torrent feed"),
            BotCommand("list", "View active feed subscriptions"),
            BotCommand("myfeeds", "View your subscriptions across all chats"),
            BotCommand("pause", "Pause notifications for a feed or 'all'"),
            BotCommand("resume", "Resume notifications for a feed or 'all'"),
            BotCommand("remove", "Unsubscribe from a feed"),
            BotCommand("filter", "Configure keyword filters (inc=/exc=)"),
            BotCommand("check", "Force an immediate feed check"),
            BotCommand("test", "Preview sample torrent notification"),
            BotCommand("id", "Show Chat ID, Topic ID, and User ID"),
            BotCommand("help", "Show help and usage guide"),
        ]
        try:
            await self.application.bot.set_my_commands(commands)
            logger.info("Bot commands successfully registered with Telegram Menu")
        except Exception as e:
            logger.warning("Could not register bot commands in Telegram: %s", e)

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
            "➕ /add [title] &lt;url&gt; [-inf &lt;include&gt;] [-exf &lt;exclude&gt;] [chat/channel] [topic_id]\n"
            "📋 /list [all] [chat/channel] — View your subscribed feeds (or all feeds if admin)\n"
            "👤 /myfeeds — View all your subscriptions across all chats\n"
            "⏸️ /pause &lt;id_or_all&gt; [chat/channel] — Pause notifications\n"
            "▶️ /resume &lt;id_or_all&gt; [chat/channel] — Resume notifications\n"
            "➖ /remove &lt;id&gt; [chat/channel] — Unsubscribe from a feed\n"
            "🔍 /filter &lt;id&gt; -inf &lt;words&gt; -exf &lt;words&gt; — Keyword filters\n"
            "🔄 /check — Force an immediate feed check\n"
            "🧪 /test [chat/channel] — Preview sample torrent notification\n"
            "🆔 /id — Display Chat ID, Topic ID, and your User ID\n\n"
            "<b>Examples:</b>\n"
            "• Subscribe with custom title & include filter:\n"
            "  /add PSA https://www.limetorrents.fun/rss/ -inf PSA\n"
            "• Subscribe a channel with include & exclude filters:\n"
            "  /add https://example.com/rss/ -inf 1080p,flac -exf 720p,cam @MyChannel\n"
            "• Pause or resume notifications:\n"
            "  /pause 1 or /pause all\n"
            "  /resume 1 or /resume all\n"
            "• Update filters for feed #1:\n"
            "  /filter 1 -inf PSA,1080p -exf 720p\n"
            "• List your feeds in current chat:\n"
            "  /list\n"
            "• List all your feeds across all chats:\n"
            "  /myfeeds"
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

    @staticmethod
    def _parse_add_args(
        args: List[str],
    ) -> Tuple[
        Optional[str],
        Optional[str],
        Optional[str],
        Optional[str],
        Optional[str],
        Optional[str],
    ]:
        """
        Parse flexible feed arguments supporting custom titles and -inf/-exf flags:
        /add PSA https://example.com/rss/ -inf PSA -exf 720p @channel
        Returns (url, custom_title, target_arg, thread_arg, inc_filter, exc_filter).
        """
        if not args:
            return None, None, None, None, None, None

        url = None
        url_idx = -1
        for idx, arg in enumerate(args):
            clean_arg = arg.strip()
            if clean_arg.startswith("http://") or clean_arg.startswith("https://"):
                url = clean_arg
                url_idx = idx
                break

        if not url or url_idx == -1:
            return None, None, None, None, None, None

        custom_title = " ".join(args[:url_idx]).strip() or None

        target_arg = None
        thread_arg = None
        inc_filter = None
        exc_filter = None

        rem = args[url_idx + 1 :]
        i = 0
        while i < len(rem):
            token = rem[i].strip()
            lower = token.lower()

            if lower in (
                "-inf",
                "--inf",
                "-inc",
                "--inc",
                "-include",
                "--include",
            ):
                i += 1
                collected = []
                while i < len(rem) and not rem[i].startswith(
                    (
                        "-exf",
                        "--exf",
                        "-exc",
                        "--exc",
                        "-exclude",
                        "--exclude",
                        "@",
                        "-100",
                    )
                ):
                    collected.append(rem[i].strip())
                    i += 1
                if collected:
                    inc_filter = " ".join(collected)
                continue

            elif lower.startswith(("inc=", "inf=", "-inf=", "-inc=")):
                inc_filter = token.split("=", 1)[1].strip()
                i += 1
                continue

            elif lower in (
                "-exf",
                "--exf",
                "-exc",
                "--exc",
                "-exclude",
                "--exclude",
            ):
                i += 1
                collected = []
                while i < len(rem) and not rem[i].startswith(
                    (
                        "-inf",
                        "--inf",
                        "-inc",
                        "--inc",
                        "-include",
                        "--include",
                        "@",
                        "-100",
                    )
                ):
                    collected.append(rem[i].strip())
                    i += 1
                if collected:
                    exc_filter = " ".join(collected)
                continue

            elif lower.startswith(("exc=", "exf=", "-exf=", "-exc=")):
                exc_filter = token.split("=", 1)[1].strip()
                i += 1
                continue

            elif token.startswith("@") or (
                token.startswith("-100") and token[1:].isdigit()
            ):
                target_arg = token
                if i + 1 < len(rem) and rem[i + 1].strip().isdigit():
                    thread_arg = rem[i + 1].strip()
                    i += 2
                    continue
                i += 1
                continue

            elif (
                (token.startswith("-") and token[1:].isdigit())
                or (len(token) >= 7 and token.isdigit())
            ) and not target_arg:
                target_arg = token
                if i + 1 < len(rem) and rem[i + 1].strip().isdigit():
                    thread_arg = rem[i + 1].strip()
                    i += 2
                    continue
                i += 1
                continue

            elif token.isdigit() and target_arg and not thread_arg:
                thread_arg = token
                i += 1
                continue

            i += 1

        return url, custom_title, target_arg, thread_arg, inc_filter, exc_filter

    async def cmd_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Subscribe a chat, channel, or topic to an RSS feed."""
        if not await self.check_authorization(update, context):
            return

        user = update.effective_user
        user_id = user.id if user else 0
        user_name = user.full_name or user.first_name if user else f"User {user_id}"

        url, custom_title, target_arg, thread_arg, inc_filter, exc_filter = (
            self._parse_add_args(context.args or [])
        )
        if not url:
            await update.effective_message.reply_html(
                "⚠️ <b>Usage:</b>\n"
                "• /add [title] &lt;feed_url&gt; [-inf &lt;include&gt;] [-exf &lt;exclude&gt;] [@channel]\n\n"
                "<b>Examples:</b>\n"
                "• Subscribe with custom title & include filter:\n"
                "  /add PSA https://www.limetorrents.fun/rss/ -inf PSA\n"
                "• Subscribe a channel with filters:\n"
                "  /add https://example.com/rss/ -inf 1080p,flac -exf 720p,cam @MyChannel"
            )
            return

        # Quota verification for regular users
        if self.max_feeds_per_user > 0 and not self.is_super_user(user_id):
            active_count = await self.db.count_user_subscriptions(user_id)
            if active_count >= self.max_feeds_per_user:
                await update.effective_message.reply_html(
                    f"⚠️ <b>Feed Limit Reached:</b> You have reached your limit of "
                    f"<b>{self.max_feeds_per_user}</b> active feed subscriptions.\n\n"
                    "Please remove unused feeds with /remove before adding new ones."
                )
                return

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

            feed_title = custom_title or title or url
            feed_id, effective_title, is_new = await self.db.subscribe(
                platform="telegram",
                target_id=target_id,
                url=url,
                title=feed_title,
                thread_id=thread_id,
                filter_keywords=inc_filter,
                filter_exclude=exc_filter,
                user_id=user_id,
            )

            # Signal poller to register and track
            self.poller.trigger_immediate_check()

            dest_str = f"<b>{html.escape(target_title)}</b> (<code>{target_id}</code>)"
            if thread_id:
                dest_str += f" [Topic: <code>#{thread_id}</code>]"

            subscriber_label = f"{html.escape(user_name)} (<code>{user_id}</code>)"

            filter_lines = []
            if inc_filter:
                filter_lines.append(
                    f"🔍 <b>Include:</b> <code>{html.escape(inc_filter)}</code>"
                )
            if exc_filter:
                filter_lines.append(
                    f"🚫 <b>Exclude:</b> <code>{html.escape(exc_filter)}</code>"
                )
            filters_str = ("\n" + "\n".join(filter_lines)) if filter_lines else ""

            if is_new:
                reply_text = (
                    f"✅ <b>Successfully Subscribed!</b>\n\n"
                    f"📡 <b>Feed:</b> {html.escape(effective_title)}\n"
                    f"🆔 <b>Feed ID:</b> <code>{feed_id}</code>\n"
                    f"🎯 <b>Destination:</b> {dest_str}\n"
                    f"👤 <b>Subscriber:</b> {subscriber_label}{filters_str}\n"
                    f"🔗 <b>URL:</b> {html.escape(url)}\n"
                    f"📦 <b>Current Items:</b> {len(items)}\n\n"
                    f"🔔 FeedPulse will notify every {POLL_INTERVAL_SECONDS}s when new uploads appear."
                )
            else:
                reply_text = (
                    f"ℹ️ <b>Already Subscribed!</b>\n\n"
                    f"You are already monitoring <b>{html.escape(effective_title)}</b> (ID: <code>{feed_id}</code>) in {dest_str}.{filters_str}"
                )

            await status_msg.edit_text(
                reply_text, parse_mode="HTML", disable_web_page_preview=True
            )

        except Exception as e:
            logger.error("Error in /add command: %s", e)
            await status_msg.edit_text(
                f"❌ Error while adding feed: {html.escape(str(e))}"
            )

    async def cmd_myfeeds(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List all subscriptions across all chats for the current user."""
        if not await self.check_authorization(update, context):
            return

        user = update.effective_user
        user_id = user.id if user else 0
        subs = await self.db.list_user_all_subscriptions(user_id)

        if not subs:
            await update.effective_message.reply_html(
                "📭 <b>You have no active subscriptions anywhere.</b>\n\n"
                "Use /add &lt;feed_url&gt; to start monitoring a feed!"
            )
            return

        lines = [f"📋 <b>Your Active Subscriptions ({len(subs)}):</b>\n"]
        for s in subs:
            title = html.escape(s.get("title") or "Untitled Feed")
            url = html.escape(s.get("url", ""))
            fid = s["feed_id"]
            tgt = s["target_id"]
            tid = s.get("thread_id")
            checked = s.get("last_checked") or "Not yet checked"

            dest_label = f"<code>{tgt}</code>"
            try:
                tgt_chat = await context.bot.get_chat(
                    int(tgt) if tgt.lstrip("-").isdigit() else tgt
                )
                if tgt_chat and tgt_chat.title:
                    dest_label = (
                        f"<b>{html.escape(tgt_chat.title)}</b> (<code>{tgt}</code>)"
                    )
                elif tgt_chat and tgt_chat.username:
                    dest_label = f"@{tgt_chat.username} (<code>{tgt}</code>)"
            except Exception:
                pass

            if tid:
                dest_label += f" [Topic: <code>#{tid}</code>]"

            status_badge = " ⏸️ <i>[PAUSED]</i>" if s.get("is_paused") else ""
            lines.append(f"• <b>[{fid}] {title}</b>{status_badge}")
            lines.append(f"  🎯 Destination: {dest_label}")
            lines.append(f"  🔗 <code>{url}</code>")
            if s.get("filter_keywords"):
                lines.append(
                    f"  🔍 <i>Include: {html.escape(s['filter_keywords'])}</i>"
                )
            if s.get("filter_exclude"):
                lines.append(f"  🚫 <i>Exclude: {html.escape(s['filter_exclude'])}</i>")
            lines.append(f"  ⏱ <i>Last Check: {checked}</i>\n")

        lines.append(
            "<i>Manage:</i> /pause &lt;id&gt; | /resume &lt;id&gt; | /remove &lt;id&gt;"
        )
        await update.effective_message.reply_html(
            "\n".join(lines), disable_web_page_preview=True
        )

    async def cmd_list(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List feeds subscribed in this chat, topic, or target channel."""
        if not await self.check_authorization(update, context):
            return

        user = update.effective_user
        user_id = user.id if user else 0
        chat = update.effective_chat
        is_admin = await self.is_admin_or_super(update)

        raw_args = list(context.args or [])
        show_my = any(a.lower() in ("my", "mine") for a in raw_args)
        if show_my:
            await self.cmd_myfeeds(update, context)
            return

        show_all = any(a.lower() in ("all", "-all", "--all") for a in raw_args)
        filtered_args = [
            a for a in raw_args if a.lower() not in ("all", "-all", "--all")
        ]
        target_arg = filtered_args[0] if filtered_args else None

        # UX Enhancement: If in private DM and no target argument was specified,
        # seamlessly display all feeds owned by this user across their channels/groups/DMs!
        if chat and chat.type == "private" and not target_arg and not show_all:
            await self.cmd_myfeeds(update, context)
            return

        target_id, thread_id, target_title = await self._resolve_destination(
            update, context, target_arg
        )
        if not target_id:
            return

        # Regular users only see their own feeds; admins see all if requested
        if show_all and is_admin:
            filter_user_id = None
        else:
            filter_user_id = user_id

        subs = await self.db.list_subscriptions(
            "telegram", target_id, thread_id=thread_id, user_id=filter_user_id
        )

        dest_label = html.escape(target_title)
        if thread_id:
            dest_label += f" [Topic: #{thread_id}]"

        if not subs:
            if filter_user_id is None:
                await update.effective_message.reply_html(
                    f"📭 <b>No active subscriptions for {dest_label}.</b>\n\n"
                    "Use /add &lt;feed_url&gt; to start monitoring a feed!"
                )
            else:
                msg = (
                    f"📭 <b>You have no active subscriptions for {dest_label}.</b>\n\n"
                    "Use /add &lt;feed_url&gt; to start monitoring a feed!"
                )
                if is_admin:
                    msg += "\n\n💡 <i>Admins: use /list all to view all feeds in this destination.</i>"
                await update.effective_message.reply_html(msg)
            return

        header = (
            f"📋 <b>All Subscriptions for {dest_label} ({len(subs)}):</b>\n"
            if filter_user_id is None
            else f"📋 <b>Your Subscriptions for {dest_label} ({len(subs)}):</b>\n"
        )
        lines = [header]
        for s in subs:
            title = html.escape(s.get("title") or "Untitled Feed")
            url = html.escape(s.get("url", ""))
            fid = s["feed_id"]
            checked = s.get("last_checked") or "Not yet checked"
            tid = s.get("thread_id")
            sub_uid = s.get("user_id")

            status_badge = " ⏸️ <i>[PAUSED]</i>" if s.get("is_paused") else ""
            topic_badge = f" [Topic: <code>#{tid}</code>]" if tid else ""
            lines.append(f"• <b>[{fid}] {title}</b>{topic_badge}{status_badge}")
            lines.append(f"  🔗 <code>{url}</code>")
            if filter_user_id is None and sub_uid:
                lines.append(f"  👤 <i>Subscriber ID: <code>{sub_uid}</code></i>")
            if s.get("filter_keywords"):
                lines.append(
                    f"  🔍 <i>Include: {html.escape(s['filter_keywords'])}</i>"
                )
            if s.get("filter_exclude"):
                lines.append(f"  🚫 <i>Exclude: {html.escape(s['filter_exclude'])}</i>")
            lines.append(f"  ⏱ <i>Last Check: {checked}</i>\n")

        lines.append(
            "<i>Manage:</i> /pause &lt;id&gt; | /resume &lt;id&gt; | /remove &lt;id&gt;"
        )
        await update.effective_message.reply_html(
            "\n".join(lines), disable_web_page_preview=True
        )

    async def cmd_remove(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Unsubscribe from a feed with ownership verification."""
        if not await self.check_authorization(update, context):
            return

        if not context.args:
            await update.effective_message.reply_html(
                "⚠️ <b>Usage:</b>\n"
                "• /remove &lt;feed_id_or_url&gt; (current chat/topic)\n"
                "• /remove &lt;feed_id_or_url&gt; &lt;@channel_or_chat_id&gt;\n\n"
                "Check your feeds with /list first."
            )
            return

        user = update.effective_user
        user_id = user.id if user else 0
        is_admin = await self.is_admin_or_super(update)

        feed_identifier = context.args[0].strip()
        target_arg = context.args[1] if len(context.args) > 1 else None

        target_id, thread_id, target_title = await self._resolve_destination(
            update, context, target_arg
        )
        if not target_id:
            return

        removed_name, status = await self.db.unsubscribe(
            "telegram",
            target_id,
            feed_identifier,
            thread_id=thread_id,
            user_id=user_id,
            is_admin=is_admin,
        )

        dest_str = html.escape(target_title)
        if thread_id:
            dest_str += f" [Topic: #{thread_id}]"

        if status == "success":
            await update.effective_message.reply_html(
                f"🗑 <b>Removed subscription:</b>\n"
                f"<b>{html.escape(removed_name or feed_identifier)}</b> has been unsubscribed from {dest_str}."
            )
        elif status == "permission_denied":
            await update.effective_message.reply_html(
                f"⛔ <b>Permission Denied:</b> Feed <b>{html.escape(removed_name or feed_identifier)}</b> "
                f"was subscribed by another user in {dest_str}.\n\n"
                "You can only remove feeds you added. Group Admins and Bot Owners can remove any feed."
            )
        else:
            await update.effective_message.reply_html(
                f"❌ Subscription not found for <code>{html.escape(feed_identifier)}</code> in {dest_str}.\n"
                "Use /list to see your active feed IDs."
            )

    async def cmd_pause(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Pause notifications for a specific feed or 'all' feeds."""
        await self._handle_pause_resume(update, context, pause=True)

    async def cmd_resume(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Resume notifications for a specific feed or 'all' feeds."""
        await self._handle_pause_resume(update, context, pause=False)

    async def _handle_pause_resume(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, pause: bool
    ) -> None:
        action_verb = "pause" if pause else "resume"
        past_verb = "paused" if pause else "resumed"
        action_icon = "⏸️" if pause else "▶️"

        if not await self.check_authorization(update, context):
            return

        if not context.args:
            await update.effective_message.reply_html(
                f"⚠️ <b>Usage:</b>\n"
                f"• /{action_verb} &lt;feed_id_or_url&gt; (e.g. /{action_verb} 1)\n"
                f"• /{action_verb} all (pause/resume all your feeds)\n"
                f"• /{action_verb} &lt;feed_id&gt; &lt;@channel_or_chat_id&gt;\n\n"
                f"Check your feeds with /list."
            )
            return

        user = update.effective_user
        user_id = user.id if user else 0
        is_admin = await self.is_admin_or_super(update)

        feed_identifier = context.args[0].strip()
        target_arg = context.args[1] if len(context.args) > 1 else None

        target_id, thread_id, target_title = await self._resolve_destination(
            update, context, target_arg
        )
        if not target_id:
            return

        dest_str = html.escape(target_title)
        if thread_id:
            dest_str += f" [Topic: #{thread_id}]"

        feed_label, status, count = await self.db.set_subscription_pause(
            "telegram",
            target_id,
            feed_identifier,
            paused=pause,
            thread_id=thread_id,
            user_id=user_id,
            is_admin=is_admin,
        )

        if status == "success":
            if feed_identifier.lower() == "all":
                await update.effective_message.reply_html(
                    f"{action_icon} <b>Successfully {past_verb.capitalize()}!</b>\n"
                    f"Updated <b>{count}</b> feed subscription(s) in {dest_str}."
                )
            else:
                await update.effective_message.reply_html(
                    f"{action_icon} <b>Feed {past_verb.capitalize()}:</b>\n"
                    f"<b>{html.escape(feed_label or feed_identifier)}</b> has been {past_verb} for {dest_str}.\n\n"
                    f"<i>To {'resume' if pause else 'pause'} anytime:</i> /{'resume' if pause else 'pause'} {html.escape(feed_identifier)}"
                )
        elif status in ("already_paused", "already_resumed"):
            target_state = "paused" if status == "already_paused" else "active/resumed"
            name_str = (
                f"<b>{html.escape(feed_label or feed_identifier)}</b>"
                if feed_identifier.lower() != "all"
                else "Your feed(s)"
            )
            is_are = "is" if feed_identifier.lower() != "all" else "are"
            await update.effective_message.reply_html(
                f"ℹ️ {name_str} in {dest_str} {is_are} already {target_state}."
            )
        elif status == "permission_denied":
            await update.effective_message.reply_html(
                f"⛔ <b>Permission Denied:</b> Feed <b>{html.escape(feed_label or feed_identifier)}</b> "
                f"was subscribed by another user in {dest_str}.\n\n"
                f"You can only {action_verb} feeds you added. Group Admins and Bot Owners can {action_verb} any feed."
            )
        else:
            await update.effective_message.reply_html(
                f"❌ Subscription not found for <code>{html.escape(feed_identifier)}</code> in {dest_str}.\n"
                f"Use /list to see your active feed IDs."
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
                "• /filter &lt;feed_id&gt; -inf &lt;words&gt; [-exf &lt;words&gt;] [@channel]\n"
                "• /filter &lt;feed_id&gt; inc=&lt;words&gt; [exc=&lt;words&gt;] [@channel]\n"
                "• /filter &lt;feed_id&gt; &lt;words&gt; (simple include filter)\n\n"
                "<b>Examples:</b>\n"
                "• /filter 1 -inf PSA -exf 720p\n"
                "• /filter 1 inc=1080p,flac exc=cam,hindi\n"
                "<i>To clear filters:</i> /filter 1 clear"
            )
            return

        user = update.effective_user
        user_id = user.id if user else 0
        is_admin = await self.is_admin_or_super(update)

        feed_id_str = context.args[0].strip()
        if not feed_id_str.isdigit():
            await update.effective_message.reply_html(
                "❌ Please provide a numeric feed ID (e.g. /filter 1 ...)."
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
            i = 0
            while i < len(filter_parts):
                part = filter_parts[i].strip()
                plow = part.lower()
                if plow in (
                    "-inf",
                    "--inf",
                    "-inc",
                    "--inc",
                    "-include",
                    "--include",
                ):
                    i += 1
                    collected = []
                    while i < len(filter_parts) and not filter_parts[
                        i
                    ].lower().startswith(
                        ("-exf", "--exf", "-exc", "--exc", "-exclude", "--exclude")
                    ):
                        collected.append(filter_parts[i].strip())
                        i += 1
                    if collected:
                        inc_val = " ".join(collected)
                    continue
                elif plow in (
                    "-exf",
                    "--exf",
                    "-exc",
                    "--exc",
                    "-exclude",
                    "--exclude",
                ):
                    i += 1
                    collected = []
                    while i < len(filter_parts) and not filter_parts[
                        i
                    ].lower().startswith(
                        ("-inf", "--inf", "-inc", "--inc", "-include", "--include")
                    ):
                        collected.append(filter_parts[i].strip())
                        i += 1
                    if collected:
                        exc_val = " ".join(collected)
                    continue
                elif plow.startswith(("inc=", "inf=", "-inf=", "-inc=")):
                    inc_val = part.split("=", 1)[1].strip()
                elif plow.startswith(("exc=", "exf=", "-exf=", "-exc=")):
                    exc_val = part.split("=", 1)[1].strip()
                elif inc_val is None and not part.startswith("-"):
                    inc_val = part
                i += 1

        status = await self.db.update_filters(
            platform="telegram",
            target_id=target_id,
            feed_id=int(feed_id_str),
            filter_keywords=inc_val,
            filter_exclude=exc_val,
            thread_id=thread_id,
            user_id=user_id,
            is_admin=is_admin,
        )

        dest_str = html.escape(target_title)
        if thread_id:
            dest_str += f" [Topic: #{thread_id}]"

        if status == "success":
            await update.effective_message.reply_html(
                f"✅ <b>Filter updated for Feed #{feed_id_str} ({dest_str})!</b>\n"
                f"• Include: <code>{html.escape(inc_val or 'Any')}</code>\n"
                f"• Exclude: <code>{html.escape(exc_val or 'None')}</code>"
            )
        elif status == "permission_denied":
            await update.effective_message.reply_html(
                f"⛔ <b>Permission Denied:</b> Feed #{feed_id_str} in {dest_str} was added by another user.\n\n"
                "You can only customize filter rules for feeds you subscribed to."
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
