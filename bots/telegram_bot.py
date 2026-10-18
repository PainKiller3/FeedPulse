import html
import io
import logging
import shlex
from typing import Optional, List, Set, Tuple, Dict
import aiohttp
from telegram import BotCommand, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

from database.db import Database
from parser.feed_parser import FeedParser, FeedItem, format_bytes, parse_size_bytes
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
        self.authorized_threads: Dict[int, Set[int]] = {}
        self.sudo_users: Set[int] = set(sudo_users or [])
        self.authorized_users: Set[int] = set()
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
        """Check if caller has bot superuser, authorized user, or group admin privileges."""
        user = update.effective_user
        chat = update.effective_chat
        if not user:
            return False
        if self.is_super_user(user.id) or (user.id in self.authorized_users):
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

        # Load persisted authorizations from MongoDB
        try:
            stored_auths = await self.db.get_all_authorizations()
            for auth in stored_auths:
                t_id = auth.get("target_id")
                if not t_id:
                    continue
                th_id = auth.get("thread_id")
                id_type = auth.get("id_type", "chat")
                if id_type == "sudo":
                    self.sudo_users.add(t_id)
                elif id_type == "user":
                    self.authorized_users.add(t_id)
                else:
                    self.authorized_chats.add(t_id)
                    if th_id is not None:
                        self.authorized_threads.setdefault(t_id, set()).add(th_id)
            if stored_auths:
                logger.info(
                    "Loaded %d authorizations from database (Total chats: %d, users: %d, sudo: %d)",
                    len(stored_auths),
                    len(self.authorized_chats),
                    len(self.authorized_users),
                    len(self.sudo_users),
                )
        except Exception as e:
            logger.warning("Could not load authorizations from database: %s", e)

        # Load rich text chat settings
        try:
            rt_settings = await self.db.get_all_chat_settings("rich_text")
            if rt_settings:
                self.dispatcher.set_rich_text_overrides(rt_settings)
                logger.info("Loaded rich text settings for %d chats", len(rt_settings))
        except Exception as e:
            logger.warning("Could not load rich text settings from database: %s", e)

        # Register command handlers
        self.application.add_handler(CommandHandler(["start", "help"], self.cmd_help))
        self.application.add_handler(
            CommandHandler(["menu", "panel", "control", "rss"], self.cmd_rss_menu)
        )
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
        self.application.add_handler(CommandHandler("edit", self.cmd_edit))
        self.application.add_handler(CommandHandler("filter", self.cmd_filter))
        self.application.add_handler(
            CommandHandler(["get", "preview", "inspect"], self.cmd_get)
        )
        self.application.add_handler(
            CommandHandler(["authorize", "a"], self.cmd_authorize)
        )
        self.application.add_handler(
            CommandHandler(["unauthorize", "ua"], self.cmd_unauthorize)
        )
        self.application.add_handler(
            CommandHandler(
                ["authlist", "authorizations", "authorized"], self.cmd_authlist
            )
        )
        self.application.add_handler(CommandHandler("addsudo", self.cmd_addsudo))
        self.application.add_handler(CommandHandler("rmsudo", self.cmd_rmsudo))
        self.application.add_handler(
            CommandHandler(["richtext", "rt"], self.cmd_richtext)
        )
        self.application.add_handler(CommandHandler("check", self.cmd_check))
        self.application.add_handler(CommandHandler("test", self.cmd_test))
        self.application.add_handler(
            CallbackQueryHandler(self.handle_menu_callback, pattern="^rss_")
        )

        async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
            logger.error("Telegram application error: %s", context.error)

        self.application.add_error_handler(on_error)

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
            BotCommand("menu", "Open interactive Control Center"),
            BotCommand("add", "Subscribe to an RSS/torrent feed"),
            BotCommand("list", "View active feed subscriptions"),
            BotCommand("myfeeds", "View your subscriptions across all chats"),
            BotCommand("get", "Inspect live feed items immediately"),
            BotCommand("pause", "Pause notifications for a feed or 'all'"),
            BotCommand("resume", "Resume notifications for a feed or 'all'"),
            BotCommand("edit", "Edit a feed subscription in-place"),
            BotCommand("remove", "Unsubscribe from a feed"),
            BotCommand("filter", "Configure keyword filters (inc=/exc=)"),
            BotCommand("richtext", "Toggle expandable rich text formatting (/rt)"),
            BotCommand("authorize", "Authorize a user, chat, or topic (/a)"),
            BotCommand("unauthorize", "Revoke authorization (/ua)"),
            BotCommand("authlist", "View all authorized users and chats"),
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
        # If no access controls are set (OWNER_ID=0, no SUDO, no AUTH_CHATS, no AUTH_USERS), open mode
        if (
            not self.owner_id
            and not self.sudo_users
            and not self.authorized_chats
            and not self.authorized_users
        ):
            return True

        user = update.effective_user
        chat = update.effective_chat
        user_id = user.id if user else 0
        chat_id = chat.id if chat else 0

        # Owner and Sudo users have global superuser access
        if (self.owner_id and user_id == self.owner_id) or (user_id in self.sudo_users):
            return True

        # In private DM: allowed if user is in authorized_users or authorized_chats
        if chat and chat.type == "private":
            if user_id in self.authorized_users or user_id in self.authorized_chats:
                return True
            await update.effective_message.reply_html(
                f"⛔ <b>Access Denied:</b> You are not authorized to control this bot.\n\n"
                f"👤 <b>Your User ID:</b> <code>{user_id}</code>\n"
                "Ask the Bot Owner to authorize you via <code>/authorize</code>."
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

        # If specific topics in this chat are restricted
        if chat_id in self.authorized_threads and self.authorized_threads[chat_id]:
            msg = update.effective_message
            is_topic = getattr(msg, "is_topic_message", False)
            thread_id = msg.message_thread_id if is_topic else None
            if thread_id and thread_id not in self.authorized_threads[chat_id]:
                logger.warning(
                    "Unauthorized topic attempt in chat %s topic %s by user %s",
                    chat_id,
                    thread_id,
                    user_id,
                )
                return False

        # If user is explicitly in authorized_users list
        if user_id in self.authorized_users:
            return True

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
            "🎛️ /menu — Open interactive Control Center\n"
            "➕ /add [title] &lt;url&gt; [-inf &lt;include&gt;] [-exf &lt;exclude&gt;] [chat/channel] [topic_id]\n"
            "📋 /list [all] [chat/channel] — View your subscribed feeds (or all feeds if admin)\n"
            "👤 /myfeeds — View all your subscriptions across all chats\n"
            "🔍 /get &lt;id_or_url&gt; [count] — Inspect live feed items right now\n"
            "⏸️ /pause &lt;id_or_all&gt; [chat/channel] — Pause notifications\n"
            "▶️ /resume &lt;id_or_all&gt; [chat/channel] — Resume notifications\n"
            "✏️ /edit &lt;id&gt; [-title &lt;name&gt;] [-inf &lt;include&gt;] [-exf &lt;exclude&gt;] [-url &lt;new_url&gt;] — Edit feed in-place\n"
            "➖ /remove &lt;id&gt; [chat/channel] — Unsubscribe from a feed\n"
            "🔍 /filter &lt;id&gt; -inf &lt;words&gt; -exf &lt;words&gt; — Keyword filters\n"
            "🎨 /richtext [on/off] — Toggle expandable quotes & rich cards (/rt)\n"
            "🔄 /check — Force an immediate feed check\n"
            "🧪 /test [chat/channel] — Preview sample torrent notification\n"
            "🆔 /id — Display Chat ID, Topic ID, and your User ID\n\n"
            "<b>Examples:</b>\n"
            "• Open the interactive Control Center:\n"
            "  /menu\n"
            "• Subscribe with custom title & include filter:\n"
            "  /add PSA https://www.limetorrents.fun/rss/ -inf PSA\n"
            "• Subscribe a channel with include & exclude filters:\n"
            "  /add https://example.com/rss/ -inf 1080p,flac -exf 720p,cam @MyChannel\n"
            "• Advanced boolean filter (1080p OR 720p) AND PSA AND x265:\n"
            "  /add https://example.com/rss/ -inf 1080p or 720p | PSA | x265\n"
            "• Case-sensitive filter:\n"
            "  /add https://example.com/rss/ -inf PSA -stv true\n"
            "• Pause or resume notifications:\n"
            "  /pause 1 or /pause all\n"
            "  /resume 1 or /resume all\n"
            "• Edit feed settings in-place:\n"
            "  /edit 1 -title PSA Movies -inf 1080p,flac -exf 720p,cam\n"
            "• Update filters for feed #1:\n"
            "  /filter 1 -inf PSA,1080p -exf 720p\n"
            "• List your feeds in current chat:\n"
            "  /list\n"
            "• List all your feeds across all chats:\n"
            "  /myfeeds"
        )
        user = update.effective_user
        user_id = user.id if user else 0
        reply_markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🎛️ Open Control Center",
                        callback_data=f"rss_main_{user_id}",
                    )
                ]
            ]
        )
        await update.effective_message.reply_html(
            text, reply_markup=reply_markup, disable_web_page_preview=True
        )

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
        bool,
        Optional[int],
        Optional[int],
    ]:
        """
        Parse flexible feed arguments supporting custom titles and -inf/-exf flags:
        /add PSA https://example.com/rss/ -inf PSA -exf 720p @channel
        Returns (url, custom_title, target_arg, thread_arg, inc_filter, exc_filter, case_sensitive, max_size, min_size).
        """
        if not args:
            return None, None, None, None, None, None, False, None, None

        url = None
        url_idx = -1
        for idx, arg in enumerate(args):
            clean_arg = arg.strip()
            if clean_arg.startswith("http://") or clean_arg.startswith("https://"):
                url = clean_arg
                url_idx = idx
                break

        if not url or url_idx == -1:
            return None, None, None, None, None, None, False, None, None

        custom_title = " ".join(args[:url_idx]).strip() or None

        target_arg = None
        thread_arg = None
        inc_filter = None
        exc_filter = None
        case_sensitive = False
        max_size = None
        min_size = None

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

            elif lower in ("-stv", "--stv", "-cs", "--cs"):
                i += 1
                if i < len(rem) and rem[i].lower() in (
                    "true",
                    "1",
                    "yes",
                    "false",
                    "0",
                    "no",
                ):
                    case_sensitive = rem[i].lower() in ("true", "1", "yes")
                    i += 1
                else:
                    case_sensitive = True
                continue

            elif lower.startswith(("stv=", "cs=", "sensitive=")):
                case_sensitive = lower.split("=", 1)[1] in ("true", "1", "yes")
                i += 1
                continue

            elif lower in ("-maxsize", "--maxsize", "-max", "--max"):
                i += 1
                if i < len(rem):
                    max_size = parse_size_bytes(rem[i])
                    i += 1
                continue

            elif lower.startswith(("maxsize=", "max=")):
                val = token.split("=", 1)[1].strip()
                max_size = parse_size_bytes(val)
                i += 1
                continue

            elif lower in ("-minsize", "--minsize", "-min", "--min"):
                i += 1
                if i < len(rem):
                    min_size = parse_size_bytes(rem[i])
                    i += 1
                continue

            elif lower.startswith(("minsize=", "min=")):
                val = token.split("=", 1)[1].strip()
                min_size = parse_size_bytes(val)
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

        return (
            url,
            custom_title,
            target_arg,
            thread_arg,
            inc_filter,
            exc_filter,
            case_sensitive,
            max_size,
            min_size,
        )

    async def cmd_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Subscribe a chat, channel, or topic to one or more RSS feeds."""
        if not await self.check_authorization(update, context):
            return

        user = update.effective_user
        user_id = user.id if user else 0
        user_name = user.full_name or user.first_name if user else f"User {user_id}"

        raw_text = update.effective_message.text or ""
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]

        if lines:
            first_parts = lines[0].split(maxsplit=1)
            if first_parts and first_parts[0].lower().startswith("/add"):
                if len(first_parts) > 1:
                    lines[0] = first_parts[1].strip()
                else:
                    lines.pop(0)

        if not lines:
            await update.effective_message.reply_html(
                "⚠️ <b>Usage:</b>\n"
                "• <code>/add [title] &lt;feed_url&gt; [-inf &lt;include&gt;] [-exf &lt;exclude&gt;] [-maxsize &lt;size&gt;] [-minsize &lt;size&gt;] [@channel]</code>\n\n"
                "<b>Batch Multi-Line Adding:</b>\n"
                "You can add multiple feeds in a single message (one per line):\n"
                "<code>/add\n"
                "PSA https://example.com/rss1 -inf 1080p\n"
                "4K https://example.com/rss2 -minsize 5GB\n"
                "Anime https://example.com/rss3 -exf 720p</code>"
            )
            return

        # If multiple lines are provided, handle as batch
        if len(lines) > 1:
            await self._handle_batch_add(update, context, lines, user_id, user_name)
            return

        try:
            tokens = shlex.split(lines[0])
        except ValueError:
            tokens = lines[0].split()

        (
            url,
            custom_title,
            target_arg,
            thread_arg,
            inc_filter,
            exc_filter,
            case_sensitive,
            max_size,
            min_size,
        ) = self._parse_add_args(tokens)
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
                custom_title=custom_title,
                case_sensitive=case_sensitive,
                max_size=max_size,
                min_size=min_size,
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
            if case_sensitive:
                filter_lines.append("🔤 <b>Case-Sensitive:</b> <code>Enabled</code>")
            if min_size:
                filter_lines.append(
                    f"📏 <b>Min Size:</b> <code>{format_bytes(min_size)}</code>"
                )
            if max_size:
                filter_lines.append(
                    f"📏 <b>Max Size:</b> <code>{format_bytes(max_size)}</code>"
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

    async def _handle_batch_add(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        lines: List[str],
        user_id: int,
        user_name: str,
    ) -> None:
        """Handle multiple feed subscriptions in a single message."""
        status_msg = await update.effective_message.reply_html(
            f"⏳ <i>Processing batch subscription of <b>{len(lines)}</b> feeds...</i>"
        )

        results: List[str] = []
        raw_results: List[str] = []
        added_count = 0

        async with aiohttp.ClientSession() as session:
            for idx, line in enumerate(lines, 1):
                try:
                    tokens = shlex.split(line)
                except ValueError:
                    tokens = line.split()

                (
                    url,
                    custom_title,
                    target_arg,
                    thread_arg,
                    inc_filter,
                    exc_filter,
                    case_sensitive,
                    max_size,
                    min_size,
                ) = self._parse_add_args(tokens)

                if not url:
                    results.append(
                        f"❌ <b>#{idx}:</b> No valid URL found in <code>{html.escape(line[:35])}...</code>"
                    )
                    raw_results.append(f"#{idx}: No valid URL found in '{line}'")
                    continue

                # Quota verification for regular users
                if self.max_feeds_per_user > 0 and not self.is_super_user(user_id):
                    active_count = await self.db.count_user_subscriptions(user_id)
                    if active_count >= self.max_feeds_per_user:
                        results.append(
                            f"⚠️ <b>#{idx}:</b> User limit reached ({self.max_feeds_per_user} max)"
                        )
                        raw_results.append(f"#{idx}: Quota exceeded for user")
                        continue

                target_id, thread_id, target_title = await self._resolve_destination(
                    update, context, target_arg, thread_arg
                )
                if not target_id:
                    results.append(
                        f"❌ <b>#{idx}:</b> Cannot access destination {html.escape(target_arg or 'current chat')}"
                    )
                    raw_results.append(f"#{idx}: Cannot access target destination")
                    continue

                try:
                    (
                        title,
                        etag,
                        modified,
                        items,
                        status,
                    ) = await FeedParser.fetch_and_parse(session, url)
                    if status != 200 or not items:
                        results.append(
                            f"❌ <b>#{idx}:</b> Failed to load feed (HTTP {status}) from <code>{html.escape(url[:30])}...</code>"
                        )
                        raw_results.append(f"#{idx}: HTTP {status} loading {url}")
                        continue

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
                        custom_title=custom_title,
                        case_sensitive=case_sensitive,
                        max_size=max_size,
                        min_size=min_size,
                    )
                    added_count += 1

                    details = []
                    if inc_filter:
                        details.append(f"+{inc_filter}")
                    if exc_filter:
                        details.append(f"-{exc_filter}")
                    if max_size:
                        details.append(f"max:{format_bytes(max_size)}")
                    if min_size:
                        details.append(f"min:{format_bytes(min_size)}")
                    extra_str = (
                        f" [<code>{' '.join(details)}</code>]" if details else ""
                    )

                    status_icon = "✅" if is_new else "ℹ️"
                    results.append(
                        f"{status_icon} <b>#{idx}</b> <b>{html.escape(effective_title)}</b> (ID: <code>{feed_id}</code>){extra_str}"
                    )
                    raw_results.append(
                        f"#{idx}: {effective_title} (ID: {feed_id}) -> {url}"
                    )

                except Exception as e:
                    logger.error("Error subscribing line '%s': %s", line, e)
                    results.append(f"❌ <b>#{idx}:</b> Error: {html.escape(str(e))}")
                    raw_results.append(f"#{idx}: Error: {e}")

        # Trigger background check for newly added feeds
        if added_count > 0:
            self.poller.trigger_immediate_check()

        summary_header = f"📦 <b>Batch Subscription Summary ({added_count}/{len(lines)} added):</b>\n\n"
        full_text = summary_header + "\n".join(results)

        if len(full_text.encode("utf-8")) > 4000:
            raw_output = (
                f"Batch Subscription Summary ({added_count}/{len(lines)} added)\n"
                + "=" * 40
                + "\n"
                + "\n".join(raw_results)
            )
            with io.BytesIO(raw_output.encode("utf-8")) as out_file:
                out_file.name = f"batch_add_{len(lines)}_feeds.txt"
                await update.effective_message.reply_document(
                    document=out_file,
                    caption=f"📦 Batch subscription finished: <b>{added_count}/{len(lines)}</b> successful",
                    parse_mode="HTML",
                )
            await status_msg.delete()
        else:
            await status_msg.edit_text(
                full_text, parse_mode="HTML", disable_web_page_preview=True
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
            if s.get("case_sensitive"):
                lines.append("  🔤 <i>Case-Sensitive: Enabled</i>")
            sz_items = []
            if s.get("min_size"):
                sz_items.append(f"Min: {format_bytes(s['min_size'])}")
            if s.get("max_size"):
                sz_items.append(f"Max: {format_bytes(s['max_size'])}")
            if sz_items:
                lines.append(f"  📏 <i>Size: {', '.join(sz_items)}</i>")
            lines.append(f"  ⏱ <i>Last Check: {checked}</i>\n")

        lines.append(
            "<i>Manage:</i> /pause &lt;id&gt; | /resume &lt;id&gt; | /edit &lt;id&gt; | /remove &lt;id&gt;"
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
            if s.get("case_sensitive"):
                lines.append("  🔤 <i>Case-Sensitive: Enabled</i>")
            sz_items = []
            if s.get("min_size"):
                sz_items.append(f"Min: {format_bytes(s['min_size'])}")
            if s.get("max_size"):
                sz_items.append(f"Max: {format_bytes(s['max_size'])}")
            if sz_items:
                lines.append(f"  📏 <i>Size: {', '.join(sz_items)}</i>")
            lines.append(f"  ⏱ <i>Last Check: {checked}</i>\n")

        lines.append(
            "<i>Manage:</i> /pause &lt;id&gt; | /resume &lt;id&gt; | /edit &lt;id&gt; | /remove &lt;id&gt;"
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

    @staticmethod
    def _parse_edit_args(
        args: List[str],
    ) -> Tuple[
        Optional[str],
        Optional[str],
        Optional[str],
        Optional[str],
        Optional[str],
        Optional[str],
        Optional[str],
    ]:
        """
        Parse arguments for /edit command.
        Supports:
          /edit <feed_id> [-title <new_title>] [-inf <include>] [-exf <exclude>] [-url <new_url>] [@channel]
        Returns (feed_identifier, title, inc_filter, exc_filter, new_url, target_arg, thread_arg)
        """
        if not args:
            return None, None, None, None, None, None, None

        feed_identifier = args[0].strip()
        rem = args[1:]

        title: Optional[str] = None
        inc_filter: Optional[str] = None
        exc_filter: Optional[str] = None
        new_url: Optional[str] = None
        case_sensitive: Optional[bool] = None
        target_arg: Optional[str] = None
        thread_arg: Optional[str] = None

        i = 0
        while i < len(rem):
            token = rem[i].strip()
            lower = token.lower()

            if lower in ("-title", "--title", "-name", "--name"):
                i += 1
                collected = []
                while i < len(rem) and not rem[i].startswith(
                    (
                        "-inf",
                        "--inf",
                        "-inc",
                        "--inc",
                        "-exf",
                        "--exf",
                        "-exc",
                        "--exc",
                        "-url",
                        "--url",
                        "@",
                        "-100",
                    )
                ):
                    collected.append(rem[i].strip())
                    i += 1
                if collected:
                    title = " ".join(collected)
                continue

            elif lower.startswith(("title=", "name=", "-title=", "-name=")):
                title = token.split("=", 1)[1].strip()
                i += 1
                continue

            elif lower in (
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
                        "-title",
                        "--title",
                        "-name",
                        "--name",
                        "-exf",
                        "--exf",
                        "-exc",
                        "--exc",
                        "-url",
                        "--url",
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
                        "-title",
                        "--title",
                        "-name",
                        "--name",
                        "-inf",
                        "--inf",
                        "-inc",
                        "--inc",
                        "-url",
                        "--url",
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

            elif lower in ("-url", "--url", "-link", "--link"):
                i += 1
                if i < len(rem):
                    new_url = rem[i].strip()
                    i += 1
                continue

            elif lower.startswith(("url=", "link=", "-url=", "-link=")):
                new_url = token.split("=", 1)[1].strip()
                i += 1
                continue

            elif lower in ("-stv", "--stv", "-cs", "--cs"):
                i += 1
                if i < len(rem) and rem[i].lower() in (
                    "true",
                    "1",
                    "yes",
                    "false",
                    "0",
                    "no",
                ):
                    case_sensitive = rem[i].lower() in ("true", "1", "yes")
                    i += 1
                else:
                    case_sensitive = True
                continue

            elif lower.startswith(("stv=", "cs=", "sensitive=")):
                case_sensitive = lower.split("=", 1)[1] in ("true", "1", "yes")
                i += 1
                continue

            elif lower in ("-maxsize", "--maxsize", "-max", "--max"):
                i += 1
                if i < len(rem):
                    val = rem[i].strip().lower()
                    max_size = (
                        -1
                        if val in ("none", "clear", "0")
                        else parse_size_bytes(rem[i])
                    )
                    i += 1
                continue

            elif lower.startswith(("maxsize=", "max=")):
                val = token.split("=", 1)[1].strip().lower()
                max_size = (
                    -1 if val in ("none", "clear", "0") else parse_size_bytes(val)
                )
                i += 1
                continue

            elif lower in ("-minsize", "--minsize", "-min", "--min"):
                i += 1
                if i < len(rem):
                    val = rem[i].strip().lower()
                    min_size = (
                        -1
                        if val in ("none", "clear", "0")
                        else parse_size_bytes(rem[i])
                    )
                    i += 1
                continue

            elif lower.startswith(("minsize=", "min=")):
                val = token.split("=", 1)[1].strip().lower()
                min_size = (
                    -1 if val in ("none", "clear", "0") else parse_size_bytes(val)
                )
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

        return (
            feed_identifier,
            title,
            inc_filter,
            exc_filter,
            new_url,
            case_sensitive,
            max_size,
            min_size,
            target_arg,
            thread_arg,
        )

    async def cmd_edit(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """
        Edit feed subscription in-place (title, include/exclude filters, or URL):
        • /edit <feed_id> -title "New Title"
        • /edit <feed_id> -inf 1080p,flac -exf cam,hindi
        • /edit <feed_id> -url https://new-feed-url.com/rss
        • /edit <feed_id> -stv true
        • /edit <feed_id> -maxsize 5GB -minsize 500MB
        • /edit <feed_id> -inf none (clear filter)
        """
        if not await self.check_authorization(update, context):
            return

        if not context.args or len(context.args) < 2:
            await update.effective_message.reply_html(
                "⚠️ <b>Usage:</b>\n"
                "• /edit &lt;feed_id&gt; -title &lt;new_name&gt;\n"
                "• /edit &lt;feed_id&gt; -inf &lt;include_words&gt; [-exf &lt;exclude_words&gt;]\n"
                "• /edit &lt;feed_id&gt; -url &lt;new_rss_url&gt;\n"
                "• /edit &lt;feed_id&gt; -stv &lt;true/false&gt;\n"
                "• /edit &lt;feed_id&gt; -maxsize &lt;size|none&gt; [-minsize &lt;size|none&gt;]\n"
                "• /edit &lt;feed_id&gt; [flags] [@channel_or_chat_id]\n\n"
                "<b>Examples:</b>\n"
                "• Change title: <code>/edit 1 -title PSA Movies</code>\n"
                "• Update filters: <code>/edit 1 -inf 1080p,flac -exf 720p,cam</code>\n"
                "• Toggle case: <code>/edit 1 -stv true</code>\n"
                "• Set size limits: <code>/edit 1 -maxsize 4GB -minsize 700MB</code>\n"
                "• Clear a filter: <code>/edit 1 -exf none</code>\n"
                "• Reset custom title: <code>/edit 1 -title none</code>"
            )
            return

        user = update.effective_user
        user_id = user.id if user else 0
        is_admin = await self.is_admin_or_super(update)

        (
            feed_identifier,
            new_title,
            inc_filter,
            exc_filter,
            new_url,
            case_sensitive,
            max_size,
            min_size,
            target_arg,
            thread_arg,
        ) = self._parse_edit_args(context.args)

        if not any(
            x is not None
            for x in (
                new_title,
                inc_filter,
                exc_filter,
                new_url,
                case_sensitive,
                max_size,
                min_size,
            )
        ):
            await update.effective_message.reply_html(
                "⚠️ <b>No changes specified!</b>\n\n"
                "Please specify at least one setting to edit using:\n"
                "• <code>-title &lt;name&gt;</code>\n"
                "• <code>-inf &lt;include keywords&gt;</code>\n"
                "• <code>-exf &lt;exclude keywords&gt;</code>\n"
                "• <code>-url &lt;new url&gt;</code>\n"
                "• <code>-stv &lt;true/false&gt;</code>\n"
                "• <code>-maxsize &lt;size|none&gt;</code>\n"
                "• <code>-minsize &lt;size|none&gt;</code>"
            )
            return

        target_id, thread_id, target_title = await self._resolve_destination(
            update, context, target_arg, thread_arg
        )
        if not target_id:
            return

        dest_str = html.escape(target_title)
        if thread_id:
            dest_str += f" [Topic: #{thread_id}]"

        feed_label, status, changed = await self.db.edit_subscription(
            platform="telegram",
            target_id=target_id,
            feed_identifier=feed_identifier,
            title=new_title,
            filter_keywords=inc_filter,
            filter_exclude=exc_filter,
            new_url=new_url,
            case_sensitive=case_sensitive,
            max_size=max_size,
            min_size=min_size,
            thread_id=thread_id,
            user_id=user_id,
            is_admin=is_admin,
        )

        if status == "success":
            lines = [
                "✏️ <b>Feed Updated Successfully!</b>\n",
                f"📡 <b>Feed:</b> {html.escape(feed_label or feed_identifier)}",
                f"🎯 <b>Destination:</b> {dest_str}\n",
                "<b>Updated Settings:</b>",
            ]
            if "title" in changed:
                lines.append(f"• 🏷 <b>Title:</b> {html.escape(changed['title'])}")
            if "filter_keywords" in changed:
                lines.append(
                    f"• 🔍 <b>Include Filter:</b> {html.escape(changed['filter_keywords'])}"
                )
            if "filter_exclude" in changed:
                lines.append(
                    f"• 🚫 <b>Exclude Filter:</b> {html.escape(changed['filter_exclude'])}"
                )
            if "url" in changed:
                lines.append(
                    f"• 🔗 <b>Feed URL:</b> <code>{html.escape(changed['url'])}</code>"
                )
            if "case_sensitive" in changed:
                lines.append(
                    f"• 🔤 <b>Case-Sensitive:</b> {html.escape(changed['case_sensitive'])}"
                )
            if "max_size" in changed:
                lines.append(
                    f"• 📏 <b>Max Size:</b> {html.escape(str(changed['max_size']))}"
                )
            if "min_size" in changed:
                lines.append(
                    f"• 📏 <b>Min Size:</b> {html.escape(str(changed['min_size']))}"
                )

            await update.effective_message.reply_html(
                "\n".join(lines), disable_web_page_preview=True
            )
            self.poller.trigger_immediate_check()

        elif status == "permission_denied":
            await update.effective_message.reply_html(
                f"⛔ <b>Permission Denied:</b> Feed <b>{html.escape(feed_label or feed_identifier)}</b> "
                f"was subscribed by another user in {dest_str}.\n\n"
                "You can only edit feeds you added. Group Admins and Bot Owners can edit any feed."
            )
        elif status == "no_changes":
            await update.effective_message.reply_html(
                f"ℹ️ No changes were made to <b>{html.escape(feed_label or feed_identifier)}</b>."
            )
        else:
            await update.effective_message.reply_html(
                f"❌ Subscription not found for <code>{html.escape(feed_identifier)}</code> in {dest_str}.\n"
                "Use /list to see your active feed IDs."
            )

    async def cmd_get(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Live Feed Inspector: Preview the latest items from a feed immediately.
        Usage: /get <feed_id_or_url> [count]
        """
        if not await self.check_authorization(update, context):
            return

        if not context.args:
            await update.effective_message.reply_html(
                "ℹ️ <b>Live Feed Inspector</b>\n\n"
                "Instantly preview the latest items from any feed without waiting for background checks.\n\n"
                "<b>Usage:</b> <code>/get &lt;feed_id or url&gt; [count]</code>\n\n"
                "<b>Examples:</b>\n"
                "• <code>/get 1</code> (previews latest 5 items for feed #1)\n"
                "• <code>/get 1 10</code> (previews latest 10 items)\n"
                "• <code>/get https://example.com/rss/ 3</code>\n\n"
                "<i>Use /list to find your feed IDs.</i>"
            )
            return

        target_input = context.args[0].strip()
        count = 5
        if len(context.args) > 1 and context.args[1].isdigit():
            count = max(1, min(int(context.args[1]), 20))

        # Determine feed URL and Title
        if target_input.startswith(("http://", "https://")):
            feed_url = target_input
            feed_label = target_input
        else:
            feed_doc = await self.db.get_feed_by_identifier(target_input)
            if not feed_doc:
                await update.effective_message.reply_html(
                    f"❌ <b>Feed not found!</b> Could not find a feed matching <code>{html.escape(target_input)}</code>.\n"
                    "Use <code>/list</code> to see your active feed IDs or provide a full URL."
                )
                return
            feed_url = feed_doc["url"]
            feed_label = feed_doc.get("title") or feed_url

        status_msg = await update.effective_message.reply_html(
            f"⏳ <i>Fetching live preview from <b>{html.escape(feed_label)}</b>...</i>"
        )

        try:
            async with aiohttp.ClientSession() as session:
                title, etag, modified, items, status = await FeedParser.fetch_and_parse(
                    session, feed_url
                )

            if status != 200 or not items:
                await status_msg.edit_text(
                    f"❌ Failed to fetch feed (HTTP {status}). Feed may be offline or empty."
                )
                return

            preview_items = items[:count]
            feed_name = title or feed_label

            lines = [
                f"📋 <b>Feed Inspector: {html.escape(feed_name)}</b>",
                f"<i>Showing latest {len(preview_items)} of {len(items)} items</i>\n",
            ]

            raw_txt_lines = [
                f"Feed: {feed_name}",
                f"URL: {feed_url}",
                f"Items: {len(preview_items)}/{len(items)}",
                "=" * 40,
                "",
            ]

            for idx, item in enumerate(preview_items, 1):
                item_title = item.title.strip()
                item_parts = [f"<b>#{idx}</b> {html.escape(item_title)}"]
                if item.size_str:
                    item_parts.append(f"📏 <b>Size:</b> {html.escape(item.size_str)}")
                if item.seeds is not None:
                    seeds_str = f"🌱 <b>Seeds:</b> {item.seeds}"
                    if item.leechers is not None:
                        seeds_str += f" | <b>Leechers:</b> {item.leechers}"
                    item_parts.append(seeds_str)
                if item.published:
                    item_parts.append(f"📅 <b>Date:</b> {html.escape(item.published)}")

                links = []
                if item.link:
                    links.append(f'<a href="{item.link}">View Page</a>')
                if item.torrent_url:
                    links.append(f'<a href="{item.torrent_url}">Download .torrent</a>')
                if links:
                    item_parts.append("🔗 " + " | ".join(links))
                if item.magnet_uri:
                    item_parts.append(f"🧲 <code>{html.escape(item.magnet_uri)}</code>")

                lines.append("\n".join(item_parts) + "\n")

                raw_txt_lines.append(f"#{idx} {item_title}")
                if item.size_str:
                    raw_txt_lines.append(f"Size: {item.size_str}")
                if item.seeds is not None:
                    raw_txt_lines.append(
                        f"Seeds: {item.seeds} | Leechers: {item.leechers}"
                    )
                if item.published:
                    raw_txt_lines.append(f"Date: {item.published}")
                if item.link:
                    raw_txt_lines.append(f"Link: {item.link}")
                if item.magnet_uri:
                    raw_txt_lines.append(f"Magnet: {item.magnet_uri}")
                raw_txt_lines.append("-" * 30)

            full_html = "\n".join(lines).strip()
            if len(full_html.encode("utf-8")) > 4000:
                raw_txt = "\n".join(raw_txt_lines)
                with io.BytesIO(raw_txt.encode("utf-8")) as out_file:
                    out_file.name = f"feed_inspector_{count}.txt"
                    await update.effective_message.reply_document(
                        document=out_file,
                        caption=f"📋 Live preview for <b>{html.escape(feed_name)}</b> ({len(preview_items)} items)",
                        parse_mode="HTML",
                    )
                await status_msg.delete()
            else:
                await status_msg.edit_text(
                    full_html,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
        except Exception as e:
            logger.error("Error inspecting feed %s: %s", feed_url, e)
            await status_msg.edit_text(f"❌ Error fetching feed: {html.escape(str(e))}")

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

    async def _build_main_menu(
        self, user_id: int, chat_id: int, chat_title: str
    ) -> Tuple[str, InlineKeyboardMarkup]:
        """Build the main RSS interactive menu text and inline keyboard."""
        chat_subs = await self.db.list_subscriptions("telegram", str(chat_id))
        user_subs = await self.db.list_user_all_subscriptions(user_id)
        role = (
            "Bot Owner"
            if user_id == self.owner_id
            else ("Sudo User" if user_id in self.sudo_users else "User")
        )

        text = (
            "⚡ <b>FeedPulse Control Center</b>\n\n"
            f"📍 <b>Location:</b> {html.escape(chat_title)}\n"
            f"📊 <b>Active in this chat:</b> {len(chat_subs)} feeds\n"
            f"👤 <b>Your total feeds:</b> {len(user_subs)} feeds\n"
            f"⏱️ <b>Check Interval:</b> every {POLL_INTERVAL_SECONDS}s\n"
            f"🛡️ <b>Role:</b> {role}\n\n"
            "<i>Select an action below to manage feeds interactively:</i>"
        )

        is_rt = self.dispatcher.is_rich_text_enabled(str(chat_id))
        rt_btn_text = "🎨 Rich Text: ON 🟢" if is_rt else "🎨 Rich Text: OFF ⚪"

        keyboard = [
            [
                InlineKeyboardButton(
                    f"📋 Chat Feeds ({len(chat_subs)})",
                    callback_data=f"rss_list_{user_id}",
                ),
                InlineKeyboardButton(
                    f"👤 My Feeds ({len(user_subs)})",
                    callback_data=f"rss_myfeeds_{user_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "⏸️ Pause All", callback_data=f"rss_pauseall_{user_id}"
                ),
                InlineKeyboardButton(
                    "▶️ Resume All", callback_data=f"rss_resumeall_{user_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    rt_btn_text, callback_data=f"rss_toggle_rt_{user_id}"
                ),
                InlineKeyboardButton(
                    "🔄 Check Now", callback_data=f"rss_check_{user_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    "🧪 Test Alert", callback_data=f"rss_test_{user_id}"
                ),
                InlineKeyboardButton(
                    "ℹ️ Help & Guide", callback_data=f"rss_help_{user_id}"
                ),
            ],
            [
                InlineKeyboardButton("❌ Close", callback_data=f"rss_close_{user_id}"),
            ],
        ]
        return text, InlineKeyboardMarkup(keyboard)

    async def cmd_rss_menu(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Display the interactive RSS Control Center menu."""
        if not await self.check_authorization(update, context):
            return

        user = update.effective_user
        chat = update.effective_chat
        user_id = user.id if user else 0
        chat_id = chat.id if chat else 0
        chat_title = chat.title or (
            f"@{chat.username}" if chat.username else f"Chat {chat_id}"
        )

        text, reply_markup = await self._build_main_menu(user_id, chat_id, chat_title)
        await update.effective_message.reply_html(
            text, reply_markup=reply_markup, disable_web_page_preview=True
        )

    async def handle_menu_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle inline button clicks from the RSS Control Center."""
        query = update.callback_query
        if not query or not query.data:
            return

        parts = query.data.split("_")
        # format: rss_<action>_<creator_user_id> or rss_toggle_rt_<creator_user_id>
        if len(parts) < 3:
            await query.answer()
            return

        action = parts[1]
        creator_id = int(parts[-1]) if parts[-1].isdigit() else 0
        user = update.effective_user
        caller_id = query.from_user.id if query.from_user else (user.id if user else 0)
        user_id = caller_id
        chat = update.effective_chat
        chat_id = chat.id if chat else 0
        chat_title = chat.title or (
            f"@{chat.username}" if chat.username else f"Chat {chat_id}"
        )

        # Strict access check: Only the user who opened this menu (or Bot Owner / Sudo) can interact
        if caller_id != creator_id and not self.is_super_user(caller_id):
            await query.answer(
                "⚠️ Not Yours! Type /menu to open your own Control Center.",
                show_alert=True,
            )
            return

        back_button = [
            [
                InlineKeyboardButton(
                    "🔙 Back to Menu", callback_data=f"rss_main_{creator_id}"
                )
            ]
        ]

        if action == "close":
            await query.answer("Menu closed.")
            # Delete user's triggering /menu command message if available
            if query.message and query.message.reply_to_message:
                try:
                    await query.message.reply_to_message.delete()
                except Exception:
                    pass

            # Delete the menu panel itself
            try:
                await query.message.delete()
            except Exception:
                try:
                    await query.edit_message_text(
                        "❌ <i>Control Center closed.</i>",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
            return

        elif action == "main":
            await query.answer()
            text, reply_markup = await self._build_main_menu(
                creator_id, chat_id, chat_title
            )
            try:
                await query.edit_message_text(
                    text,
                    reply_markup=reply_markup,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
            return

        elif action == "check":
            self.poller.trigger_immediate_check()
            await query.answer(
                "🔄 Immediate feed check cycle triggered!", show_alert=True
            )
            return

        elif action == "toggle":
            cur_rt = self.dispatcher.is_rich_text_enabled(str(chat_id))
            new_rt = not cur_rt
            await self.db.set_chat_setting(str(chat_id), "rich_text", new_rt)
            self.dispatcher.set_rich_text(str(chat_id), new_rt)
            status_text = "Enabled 🟢" if new_rt else "Disabled ⚪"
            await query.answer(f"🎨 Rich Text {status_text} for this chat!")
            text, reply_markup = await self._build_main_menu(
                creator_id, chat_id, chat_title
            )
            try:
                await query.edit_message_text(
                    text,
                    reply_markup=reply_markup,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
            return

        elif action == "test":
            await query.answer("🧪 Generating sample alert...")
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
            is_topic = getattr(query.message, "is_topic_message", False)
            thread_id = query.message.message_thread_id if is_topic else None
            await self.dispatcher.send_telegram(
                str(chat_id),
                sample_item,
                "Sample RSS Feed (Preview)",
                thread_id=thread_id,
            )
            return

        elif action == "pauseall":
            feed_label, status, count = await self.db.set_subscription_pause(
                "telegram", str(chat_id), "all", paused=True
            )
            if count > 0:
                await query.answer(
                    f"⏸️ Paused {count} feeds in this chat!", show_alert=True
                )
            else:
                await query.answer("ℹ️ No feeds to pause.", show_alert=True)
            text, reply_markup = await self._build_main_menu(
                creator_id, chat_id, chat_title
            )
            try:
                await query.edit_message_text(
                    text,
                    reply_markup=reply_markup,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
            return

        elif action == "resumeall":
            feed_label, status, count = await self.db.set_subscription_pause(
                "telegram", str(chat_id), "all", paused=False
            )
            if count > 0:
                await query.answer(
                    f"▶️ Resumed {count} feeds in this chat!", show_alert=True
                )
            else:
                await query.answer("ℹ️ No paused feeds to resume.", show_alert=True)
            text, reply_markup = await self._build_main_menu(
                creator_id, chat_id, chat_title
            )
            try:
                await query.edit_message_text(
                    text,
                    reply_markup=reply_markup,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
            return

        elif action == "list":
            await query.answer()
            chat_subs = await self.db.list_subscriptions("telegram", str(chat_id))
            if not chat_subs:
                content = (
                    "📋 <b>Feeds in this Chat:</b>\n\n"
                    "<i>No active feeds subscribed in this chat.</i>\n"
                    "Use <code>/add &lt;url&gt;</code> to subscribe!"
                )
            else:
                lines = [
                    f"📋 <b>Feeds in {html.escape(chat_title)} ({len(chat_subs)}):</b>\n"
                ]
                for sub in chat_subs[:15]:
                    status_icon = "⏸️" if sub.get("is_paused") else "🟢"
                    custom = sub.get("custom_title")
                    title = custom if custom else (sub.get("title") or "Unnamed Feed")
                    sz = ""
                    if sub.get("min_size") or sub.get("max_size"):
                        sz = f" [📏 {format_bytes(sub.get('min_size')) or '0 B'} - {format_bytes(sub.get('max_size')) or 'Any'}]"
                    lines.append(
                        f"• {status_icon} <b>#{sub['feed_id']}</b> {html.escape(title)}{sz}\n"
                        f"  <code>{html.escape(sub['url'])}</code>"
                    )
                content = "\n\n".join(lines)
                if len(chat_subs) > 15:
                    content += f"\n\n<i>...and {len(chat_subs) - 15} more. Use /list to see all.</i>"

            try:
                await query.edit_message_text(
                    content,
                    reply_markup=InlineKeyboardMarkup(back_button),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
            return

        elif action == "myfeeds":
            await query.answer()
            user_subs = await self.db.list_user_all_subscriptions(user_id)
            if not user_subs:
                content = (
                    "👤 <b>Your Subscriptions:</b>\n\n"
                    "<i>You have not added any feeds yet.</i>\n"
                    "Use <code>/add &lt;url&gt;</code> to start monitoring!"
                )
            else:
                lines = [
                    f"👤 <b>Your Subscriptions Across All Chats ({len(user_subs)}):</b>\n"
                ]
                for sub in user_subs[:15]:
                    status_icon = "⏸️" if sub.get("is_paused") else "🟢"
                    custom = sub.get("custom_title")
                    title = custom if custom else (sub.get("title") or "Unnamed Feed")
                    chat_lbl = sub.get("target_id")
                    lines.append(
                        f"• {status_icon} <b>#{sub['feed_id']}</b> {html.escape(title)} (Chat: <code>{chat_lbl}</code>)\n"
                        f"  <code>{html.escape(sub['url'])}</code>"
                    )
                content = "\n\n".join(lines)
                if len(user_subs) > 15:
                    content += f"\n\n<i>...and {len(user_subs) - 15} more. Use /myfeeds to see all.</i>"

            try:
                await query.edit_message_text(
                    content,
                    reply_markup=InlineKeyboardMarkup(back_button),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
            return

        elif action == "help":
            await query.answer()
            help_text = (
                "ℹ️ <b>FeedPulse Commands Quick Reference:</b>\n\n"
                "• <code>/menu</code> — Interactive Control Center\n"
                "• <code>/add &lt;url&gt;</code> — Add single or multi-line feeds\n"
                "• <code>/list</code> — View active subscriptions\n"
                "• <code>/get &lt;id|url&gt; [count]</code> — Live feed inspector\n"
                "• <code>/edit &lt;id&gt;</code> — In-place feed editor\n"
                "• <code>/pause &lt;id|all&gt;</code> — Pause notifications\n"
                "• <code>/resume &lt;id|all&gt;</code> — Resume notifications\n"
                "• <code>/remove &lt;id&gt;</code> — Unsubscribe\n"
                "• <code>/filter &lt;id&gt;</code> — Update keywords\n"
                "• <code>/authorize &lt;id&gt;</code> — Authorize user/chat (/a)\n"
                "• <code>/unauthorize &lt;id&gt;</code> — Revoke access (/ua)\n"
                "• <code>/authlist</code> — View authorized entities\n"
                "• <code>/check</code> — Force immediate check"
            )
            try:
                await query.edit_message_text(
                    help_text,
                    reply_markup=InlineKeyboardMarkup(back_button),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
            return

    # ---------------- Dynamic Authorization Commands ----------------

    async def cmd_authorize(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Authorize a user, group, channel, or forum topic (/authorize or /a)."""
        user = update.effective_user
        user_id = user.id if user else 0
        if not self.is_super_user(user_id):
            await update.effective_message.reply_html(
                "⛔ <b>Permission Denied:</b> Only the Bot Owner or Sudo Users can manage authorizations."
            )
            return

        reply = update.effective_message.reply_to_message
        target_id: Optional[int] = None
        thread_id: Optional[int] = None
        id_type: str = "chat"
        title: str = ""

        raw_args = context.args or []
        flags = [a.lower() for a in raw_args if a.startswith("-")]
        clean_args = [a for a in raw_args if not a.startswith("-")]

        if "-sudo" in flags:
            id_type = "sudo"
        elif "-user" in flags:
            id_type = "user"
        elif "-chat" in flags:
            id_type = "chat"

        if clean_args:
            arg = clean_args[0].strip()
            if "|" in arg:
                parts = arg.split("|", 1)
                try:
                    target_id = int(parts[0].strip())
                    thread_id = int(parts[1].strip())
                    id_type = "chat"
                except ValueError:
                    await update.effective_message.reply_html(
                        "⚠️ <b>Invalid Format!</b> Use <code>/authorize &lt;chat_id&gt;|&lt;thread_id&gt;</code>"
                    )
                    return
            else:
                try:
                    target_id = int(arg)
                    if (
                        id_type != "sudo"
                        and "-user" not in flags
                        and "-chat" not in flags
                    ):
                        id_type = "chat" if target_id < 0 else "user"
                except ValueError:
                    await update.effective_message.reply_html(
                        "⚠️ <b>Invalid ID!</b> Please provide a numeric Chat ID or User ID."
                    )
                    return

            title = f"{'Chat' if id_type == 'chat' else 'User'} {target_id}"

        elif reply and reply.from_user:
            target_id = reply.from_user.id
            if id_type != "sudo":
                id_type = "user"
            title = (
                reply.from_user.full_name or reply.from_user.username or str(target_id)
            )
            thread_id = None

        else:
            chat = update.effective_chat
            if not chat:
                return
            if chat.type == "private":
                await update.effective_message.reply_html(
                    "ℹ️ <b>Authorization Usage:</b>\n\n"
                    "• <code>/authorize</code> (inside a group/channel/topic or reply to user)\n"
                    "• <code>/authorize &lt;chat_id|user_id&gt;</code>\n"
                    "• <code>/authorize &lt;chat_id&gt;|&lt;topic_id&gt;</code>\n"
                    "• <code>/authorize &lt;user_id&gt; -sudo</code> (Add superuser)"
                )
                return
            else:
                target_id = chat.id
                id_type = "chat"
                title = chat.title or str(chat.id)
                msg = update.effective_message
                is_topic = getattr(msg, "is_topic_message", False)
                thread_id = msg.message_thread_id if is_topic else None

        if not target_id:
            await update.effective_message.reply_html(
                "⚠️ Could not resolve target ID for authorization."
            )
            return

        # Persist in MongoDB
        await self.db.add_authorization(
            target_id=target_id,
            thread_id=thread_id,
            id_type=id_type,
            title=title,
            added_by=user_id,
        )

        # Update in-memory cache
        if id_type == "sudo":
            self.sudo_users.add(target_id)
        elif id_type == "user":
            self.authorized_users.add(target_id)
        else:
            self.authorized_chats.add(target_id)
            if thread_id is not None:
                self.authorized_threads.setdefault(target_id, set()).add(thread_id)

        type_label = (
            "🛡️ Sudo Superuser"
            if id_type == "sudo"
            else ("👤 Authorized User" if id_type == "user" else "💬 Authorized Chat")
        )
        topic_info = (
            f"\n🧵 <b>Topic ID:</b> <code>{thread_id}</code>"
            if thread_id is not None
            else ""
        )
        await update.effective_message.reply_html(
            f"✅ <b>Successfully Authorized!</b>\n\n"
            f"🏷 <b>Type:</b> {type_label}\n"
            f"🆔 <b>Target ID:</b> <code>{target_id}</code>"
            f"{topic_info}\n"
            f"📝 <b>Title:</b> {html.escape(title or 'N/A')}\n\n"
            f"<i>Permissions granted immediately and saved in MongoDB.</i>"
        )

    async def cmd_unauthorize(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Revoke authorization for a user, group, channel, or topic (/unauthorize or /ua)."""
        user = update.effective_user
        user_id = user.id if user else 0
        if not self.is_super_user(user_id):
            await update.effective_message.reply_html(
                "⛔ <b>Permission Denied:</b> Only the Bot Owner or Sudo Users can manage authorizations."
            )
            return

        reply = update.effective_message.reply_to_message
        target_id: Optional[int] = None
        thread_id: Optional[int] = None

        if context.args:
            arg = context.args[0].strip()
            if "|" in arg:
                parts = arg.split("|", 1)
                try:
                    target_id = int(parts[0].strip())
                    thread_id = int(parts[1].strip())
                except ValueError:
                    await update.effective_message.reply_html(
                        "⚠️ <b>Invalid Format!</b> Use <code>/unauthorize &lt;chat_id&gt;|&lt;thread_id&gt;</code>"
                    )
                    return
            else:
                try:
                    target_id = int(arg)
                except ValueError:
                    await update.effective_message.reply_html(
                        "⚠️ <b>Invalid ID!</b> Please provide a numeric Chat ID or User ID."
                    )
                    return
        elif reply and reply.from_user:
            target_id = reply.from_user.id
            thread_id = None
        else:
            chat = update.effective_chat
            if not chat:
                return
            if chat.type == "private":
                await update.effective_message.reply_html(
                    "ℹ️ <b>Unauthorize Usage:</b>\n"
                    "• <code>/unauthorize</code> (inside chat to revoke)\n"
                    "• <code>/unauthorize &lt;id&gt;</code>\n"
                    "• <code>/unauthorize &lt;chat_id&gt;|&lt;topic_id&gt;</code>"
                )
                return
            else:
                target_id = chat.id
                msg = update.effective_message
                is_topic = getattr(msg, "is_topic_message", False)
                thread_id = msg.message_thread_id if is_topic else None

        if not target_id:
            await update.effective_message.reply_html(
                "⚠️ Target ID could not be determined."
            )
            return

        if target_id == self.owner_id:
            await update.effective_message.reply_html(
                "⚠️ Cannot unauthorize the Bot Owner!"
            )
            return

        # Delete from MongoDB
        await self.db.remove_authorization(target_id=target_id, thread_id=thread_id)

        # Update in-memory sets
        self.sudo_users.discard(target_id)
        self.authorized_users.discard(target_id)
        if thread_id is not None and target_id in self.authorized_threads:
            self.authorized_threads[target_id].discard(thread_id)
            if not self.authorized_threads[target_id]:
                del self.authorized_threads[target_id]
                self.authorized_chats.discard(target_id)
        else:
            self.authorized_chats.discard(target_id)
            self.authorized_threads.pop(target_id, None)

        topic_str = (
            f" (Topic: <code>{thread_id}</code>)" if thread_id is not None else ""
        )
        await update.effective_message.reply_html(
            f"🚫 <b>Authorization Revoked!</b>\n\n"
            f"🆔 <b>Target ID:</b> <code>{target_id}</code>{topic_str}\n"
            f"<i>This entity is no longer authorized to manage feeds.</i>"
        )

    async def cmd_authlist(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """View all authorized chats, topics, users, and sudo accounts."""
        user = update.effective_user
        user_id = user.id if user else 0
        if not self.is_super_user(user_id):
            await update.effective_message.reply_html(
                "⛔ <b>Permission Denied:</b> Only the Bot Owner or Sudo Users can view authorizations."
            )
            return

        lines = ["🛡️ <b>FeedPulse Access Control Overview</b>\n"]
        lines.append(
            f"👑 <b>Bot Owner:</b> <code>{self.owner_id or 'Not Configured'}</code>\n"
        )

        # Sudo users
        if self.sudo_users:
            sudo_list = ", ".join(
                f"<code>{uid}</code>" for uid in sorted(self.sudo_users)
            )
            lines.append(f"🛡️ <b>Sudo Users ({len(self.sudo_users)}):</b> {sudo_list}\n")
        else:
            lines.append("🛡️ <b>Sudo Users:</b> <i>None</i>\n")

        # Authorized users
        if self.authorized_users:
            user_list = ", ".join(
                f"<code>{uid}</code>" for uid in sorted(self.authorized_users)
            )
            lines.append(
                f"👤 <b>Authorized Users ({len(self.authorized_users)}):</b> {user_list}\n"
            )
        else:
            lines.append("👤 <b>Authorized Users:</b> <i>None</i>\n")

        # Authorized chats
        if self.authorized_chats:
            lines.append(f"💬 <b>Authorized Chats ({len(self.authorized_chats)}):</b>")
            for cid in sorted(self.authorized_chats):
                threads = self.authorized_threads.get(cid)
                if threads:
                    th_str = ", ".join(f"Topic {tid}" for tid in sorted(threads))
                    lines.append(f"• <code>{cid}</code> ({th_str})")
                else:
                    lines.append(f"• <code>{cid}</code> (All Topics / Full Chat)")
        else:
            lines.append(
                "💬 <b>Authorized Chats:</b> <i>None (Open Mode or Unconfigured)</i>"
            )

        await update.effective_message.reply_html("\n".join(lines))

    async def cmd_addsudo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Add a sudo superuser (Bot Owner only)."""
        user = update.effective_user
        user_id = user.id if user else 0
        if not self.owner_id or user_id != self.owner_id:
            await update.effective_message.reply_html(
                "⛔ <b>Permission Denied:</b> Only the Bot Owner can add Sudo superusers."
            )
            return

        reply = update.effective_message.reply_to_message
        target_id: Optional[int] = None
        title = ""
        if context.args:
            try:
                target_id = int(context.args[0].strip())
            except ValueError:
                await update.effective_message.reply_html("⚠️ Invalid User ID!")
                return
        elif reply and reply.from_user:
            target_id = reply.from_user.id
            title = (
                reply.from_user.full_name or reply.from_user.username or str(target_id)
            )

        if not target_id:
            await update.effective_message.reply_html(
                "ℹ️ <b>Usage:</b> <code>/addsudo &lt;user_id&gt;</code> (or reply to user)"
            )
            return

        await self.db.add_authorization(
            target_id=target_id, id_type="sudo", title=title, added_by=user_id
        )
        self.sudo_users.add(target_id)
        await update.effective_message.reply_html(
            f"🛡️ <b>Sudo User Added:</b> <code>{target_id}</code> is now a superuser."
        )

    async def cmd_rmsudo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Remove a sudo superuser (Bot Owner only)."""
        user = update.effective_user
        user_id = user.id if user else 0
        if not self.owner_id or user_id != self.owner_id:
            await update.effective_message.reply_html(
                "⛔ <b>Permission Denied:</b> Only the Bot Owner can remove Sudo superusers."
            )
            return

        if not context.args:
            await update.effective_message.reply_html(
                "ℹ️ <b>Usage:</b> <code>/rmsudo &lt;user_id&gt;</code>"
            )
            return

        try:
            target_id = int(context.args[0].strip())
        except ValueError:
            await update.effective_message.reply_html("⚠️ Invalid User ID!")
            return

        if target_id == self.owner_id:
            await update.effective_message.reply_html(
                "⚠️ Cannot remove the Bot Owner from Sudo!"
            )
            return

        await self.db.remove_authorization(target_id=target_id)
        self.sudo_users.discard(target_id)
        await update.effective_message.reply_html(
            f"🚫 <b>Sudo User Removed:</b> <code>{target_id}</code> is no longer a superuser."
        )

    # ---------------- Rich Text Configuration ----------------

    async def cmd_richtext(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Toggle or view rich text formatting for the current chat (/richtext or /rt)."""
        if not await self.check_authorization(update, context):
            return

        chat = update.effective_chat
        if not chat:
            return
        chat_id = str(chat.id)

        if not context.args:
            cur = self.dispatcher.is_rich_text_enabled(chat_id)
            state_str = (
                "🟢 <b>Enabled</b> (Expandable magnet blockquotes & metadata cards)"
                if cur
                else "⚪ <b>Disabled</b> (Standard clean layout)"
            )
            await update.effective_message.reply_html(
                f"🎨 <b>Rich Text Status for this chat:</b> {state_str}\n\n"
                "To configure:\n"
                "• <code>/richtext on</code> — Enable rich text formatting\n"
                "• <code>/richtext off</code> — Disable rich text formatting\n"
                "• <code>/richtext toggle</code> — Toggle setting\n"
                "• Or toggle anytime using the <b>/menu</b> button!"
            )
            return

        arg = context.args[0].lower().strip()
        if arg in ("on", "enable", "1", "true"):
            new_val = True
        elif arg in ("off", "disable", "0", "false"):
            new_val = False
        elif arg in ("toggle", "flip"):
            new_val = not self.dispatcher.is_rich_text_enabled(chat_id)
        else:
            await update.effective_message.reply_html(
                "⚠️ Invalid option. Use <code>/richtext on</code>, <code>/richtext off</code>, or <code>/richtext toggle</code>."
            )
            return

        await self.db.set_chat_setting(chat_id, "rich_text", new_val)
        self.dispatcher.set_rich_text(chat_id, new_val)

        state_label = (
            "🟢 <b>Enabled</b> (Expandable magnet blockquotes & metadata cards)"
            if new_val
            else "⚪ <b>Disabled</b> (Standard clean layout)"
        )
        await update.effective_message.reply_html(
            f"🎨 <b>Rich Text updated!</b>\n\n"
            f"Current state for this chat: {state_label}"
        )
