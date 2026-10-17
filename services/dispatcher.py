import html
import logging
import re
from typing import Any, Optional, Dict
from parser.feed_parser import FeedItem

try:
    from config import (
        ENABLE_RICH_TEXT,
        TORRENT_ALERT_TEMPLATE,
        FEED_ALERT_TEMPLATE,
    )
except ImportError:
    from config_sample import (
        ENABLE_RICH_TEXT,
        TORRENT_ALERT_TEMPLATE,
        FEED_ALERT_TEMPLATE,
    )

logger = logging.getLogger(__name__)


class SafeDict(dict):
    """Dictionary that returns empty string for missing template keys without raising KeyError."""

    def __missing__(self, key: str) -> str:
        return ""


class NotificationDispatcher:
    """Dispatches formatted RSS notifications to Telegram and Discord subscribers with template and rich text support."""

    def __init__(self):
        self.telegram_bot = None  # telegram.Bot instance
        self.discord_bot = None  # discord.ext.commands.Bot instance
        self.enable_rich_text: bool = bool(ENABLE_RICH_TEXT)
        self.torrent_template: str = TORRENT_ALERT_TEMPLATE or ""
        self.feed_template: str = FEED_ALERT_TEMPLATE or ""
        self.rich_text_overrides: Dict[str, bool] = {}

    def set_telegram_bot(self, bot: Any) -> None:
        self.telegram_bot = bot

    def set_discord_bot(self, bot: Any) -> None:
        self.discord_bot = bot

    def set_rich_text_overrides(self, overrides: Dict[str, bool]) -> None:
        """Set cached per-chat rich text overrides loaded from database."""
        self.rich_text_overrides = {str(k): bool(v) for k, v in overrides.items()}

    def set_rich_text(self, chat_id: str, enabled: bool) -> None:
        """Update rich text setting for a specific chat."""
        self.rich_text_overrides[str(chat_id)] = bool(enabled)

    def is_rich_text_enabled(self, chat_id: Optional[str] = None) -> bool:
        """Check if rich text formatting is enabled for a given chat."""
        if chat_id is not None and str(chat_id) in self.rich_text_overrides:
            return self.rich_text_overrides[str(chat_id)]
        return self.enable_rich_text

    # ---------------- Telegram Formatting & Sending ----------------

    def _build_template_context(
        self, item: FeedItem, feed_title: str, rich_text: bool
    ) -> SafeDict:
        """Build a safe, HTML-escaped dictionary of all template variables."""
        safe_title = html.escape(item.title)
        safe_feed = html.escape(feed_title)
        safe_cat = html.escape(item.category or "")
        safe_size = html.escape(item.size_str or "")
        seeds_str = str(item.seeds) if item.seeds is not None else "?"
        leech_str = str(item.leechers) if item.leechers is not None else "?"
        safe_magnet = html.escape(item.magnet_uri or "")
        safe_pub = html.escape(item.published or "")
        safe_desc = html.escape(item.description or "")
        # Dynamic media tags extraction for custom template variables
        res_match = re.search(
            r"\b(2160p|4k|1080p|1080i|720p|576p|480p)\b", item.title, re.IGNORECASE
        )
        safe_res = (
            "2160p"
            if res_match and res_match.group(1).lower() == "4k"
            else (res_match.group(1).lower() if res_match else "")
        )
        codec_match = re.search(
            r"\b(x265|h265|hevc|x264|h264|av1|xvid)\b", item.title, re.IGNORECASE
        )
        safe_codec = codec_match.group(1).upper() if codec_match else ""
        year_match = re.search(r"\b(19\d\d|20\d\d)\b", item.title)
        year_str = year_match.group(1) if year_match else ""

        # Format metadata card
        meta_parts = []
        if safe_cat:
            meta_parts.append(f"🏷 <b>Category:</b> {safe_cat}")
        if safe_size:
            meta_parts.append(f"📦 <b>Size:</b> {safe_size}")
        if item.seeds is not None or item.leechers is not None:
            meta_parts.append(
                f"🟢 <b>Seeds:</b> {seeds_str} | 🔴 <b>Leechers:</b> {leech_str}"
            )

        if meta_parts:
            if rich_text:
                # Sleek modern Telegram blockquote
                meta_card = "<blockquote>" + "\n".join(meta_parts) + "</blockquote>\n"
            else:
                meta_card = "\n".join(meta_parts) + "\n"
        else:
            meta_card = ""

        # Format magnet block
        if item.magnet_uri:
            if rich_text:
                # Modern Telegram expandable blockquote: prevents 15-line clutter on mobile
                magnet_block = (
                    "🧲 <b>Magnet URI:</b> <i>(tap to copy)</i>\n"
                    f"<blockquote expandable><code>{safe_magnet}</code></blockquote>"
                )
            else:
                magnet_block = (
                    "🧲 <b>Magnet URI:</b> <i>(tap to copy)</i>\n"
                    f"<code>{safe_magnet}</code>"
                )
        else:
            magnet_block = ""

        return SafeDict(
            title=safe_title,
            feed_title=safe_feed,
            size=safe_size,
            category=safe_cat,
            seeds=seeds_str,
            leechers=leech_str,
            magnet=safe_magnet,
            torrent_url=item.torrent_url or "",
            link=item.link or "",
            published=safe_pub,
            resolution=safe_res,
            codec=safe_codec,
            year=year_str,
            description=safe_desc,
            meta_card=meta_card,
            magnet_block=magnet_block,
        )

    def _render_default_telegram(
        self, item: FeedItem, context: SafeDict, rich_text: bool
    ) -> str:
        """Render built-in default Telegram notification text."""
        if item.is_torrent:
            lines = [
                "⚡ <b>New Torrent Uploaded!</b>",
                f"📡 <i>{context['feed_title']}</i>\n",
                f"🎬 <b>{context['title']}</b>\n",
            ]
            if context["meta_card"]:
                lines.append(context["meta_card"])
            if context["magnet_block"]:
                lines.append(context["magnet_block"])
            return "\n".join(lines).strip()
        else:
            lines = [
                "📰 <b>New Feed Post:</b>",
                f"📡 <i>{context['feed_title']}</i>\n",
                f"📝 <b>{context['title']}</b>\n",
            ]
            if context["published"]:
                lines.append(f"📅 <i>{context['published']}</i>\n")
            return "\n".join(lines).strip()

    def format_telegram_message(
        self,
        item: FeedItem,
        feed_title: str,
        rich_text: bool = True,
        force_default: bool = False,
    ) -> tuple:
        """Create HTML message and inline keyboard for Telegram with template and rich text support."""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        context = self._build_template_context(item, feed_title, rich_text=rich_text)
        template = None
        if not force_default:
            template = self.torrent_template if item.is_torrent else self.feed_template

        if template and template.strip():
            try:
                raw_text = template.format_map(context).strip()
                # Clean up any extraneous consecutive blank lines (3 or more)
                text = re.sub(r"\n{3,}", "\n\n", raw_text)
            except Exception as e:
                logger.warning(
                    "Error evaluating custom alert template: %s. Using default format.",
                    e,
                )
                text = self._render_default_telegram(item, context, rich_text=rich_text)
        else:
            text = self._render_default_telegram(item, context, rich_text=rich_text)

        # Inline keyboard buttons
        buttons = []
        row = []
        if item.torrent_url and item.torrent_url.startswith(("http://", "https://")):
            row.append(
                InlineKeyboardButton("📥 Download .torrent", url=item.torrent_url)
            )
        if item.link and item.link.startswith(("http://", "https://")):
            btn_label = "🌐 View Page" if item.is_torrent else "🔗 Read Article"
            row.append(InlineKeyboardButton(btn_label, url=item.link))

        if row:
            buttons.append(row)

        keyboard = InlineKeyboardMarkup(buttons) if buttons else None
        return text.strip(), keyboard

    async def send_telegram(
        self,
        chat_id: str,
        item: FeedItem,
        feed_title: str,
        thread_id: Optional[int] = None,
    ) -> bool:
        """Send a notification message to a Telegram chat, channel, or forum topic."""
        if not self.telegram_bot:
            logger.debug(
                "Telegram bot not configured; skipping Telegram alert to %s", chat_id
            )
            return False

        rich_text = self.is_rich_text_enabled(str(chat_id))
        text, reply_markup = self.format_telegram_message(
            item, feed_title, rich_text=rich_text
        )

        send_kwargs = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": reply_markup,
            "disable_web_page_preview": False,
        }
        if thread_id is not None:
            send_kwargs["message_thread_id"] = int(thread_id)

        try:
            await self.telegram_bot.send_message(**send_kwargs)
            return True
        except Exception as e:
            err_str = str(e).lower()
            # If user's template or rich formatting has an HTML syntax issue, fall back to safe default
            if (
                "can't parse entities" in err_str
                or "unsupported" in err_str
                or "tag" in err_str
            ):
                logger.warning(
                    "Telegram HTML parse error in chat %s: %s. Falling back to built-in default format.",
                    chat_id,
                    e,
                )
                try:
                    fallback_text, fallback_markup = self.format_telegram_message(
                        item, feed_title, rich_text=False, force_default=True
                    )
                    send_kwargs["text"] = fallback_text
                    send_kwargs["reply_markup"] = fallback_markup
                    await self.telegram_bot.send_message(**send_kwargs)
                    return True
                except Exception as fb_err:
                    logger.error("Failed even with fallback template: %s", fb_err)
                    return False
            else:
                logger.error(
                    "Failed to send Telegram notification to chat %s (thread: %s): %s",
                    chat_id,
                    thread_id,
                    e,
                )
                return False

    # ---------------- Discord Formatting & Sending ----------------

    def format_discord_message(self, item: FeedItem, feed_title: str):
        """Create Discord Embed and ActionRow View."""
        import discord

        color = 0x2ECC71 if item.is_torrent else 0x3498DB
        embed = discord.Embed(
            title=item.title[:256],
            url=item.link
            if item.link and item.link.startswith(("http://", "https://"))
            else None,
            color=color,
        )
        embed.set_author(name=f"FeedPulse • {feed_title[:200]}")

        if item.category:
            embed.add_field(name="🏷 Category", value=item.category[:100], inline=True)
        if item.size_str:
            embed.add_field(name="📦 Size", value=item.size_str[:50], inline=True)

        if item.seeds is not None or item.leechers is not None:
            s = item.seeds if item.seeds is not None else "?"
            leech = item.leechers if item.leechers is not None else "?"
            embed.add_field(
                name="📊 Peers", value=f"🟢 **{s}** | 🔴 **{leech}**", inline=True
            )

        if item.magnet_uri:
            # Discord field value has max 1024 characters
            mag_display = item.magnet_uri
            if len(mag_display) > 1010:
                mag_display = mag_display[:1007] + "..."
            embed.add_field(
                name="🧲 Magnet URI", value=f"```{mag_display}```", inline=False
            )

        if item.published:
            embed.set_footer(text=f"Published: {item.published}")

        # View with buttons
        view = discord.ui.View()
        has_buttons = False
        if item.torrent_url and item.torrent_url.startswith(("http://", "https://")):
            view.add_item(
                discord.ui.Button(
                    label="Download .torrent", url=item.torrent_url, emoji="📥"
                )
            )
            has_buttons = True
        if item.link and item.link.startswith(("http://", "https://")):
            view.add_item(
                discord.ui.Button(label="View Page", url=item.link, emoji="🌐")
            )
            has_buttons = True

        return embed, (view if has_buttons else None)

    async def send_discord(
        self, channel_id: str, item: FeedItem, feed_title: str
    ) -> bool:
        """Send a notification embed to a Discord channel or DM."""
        if not self.discord_bot:
            logger.debug(
                "Discord bot not configured; skipping Discord alert to %s", channel_id
            )
            return False

        try:
            cid = int(channel_id)
            channel = self.discord_bot.get_channel(cid)
            if not channel:
                # Fetch channel or user asynchronously
                try:
                    channel = await self.discord_bot.fetch_channel(cid)
                except Exception:
                    channel = await self.discord_bot.fetch_user(cid)

            if not channel:
                logger.warning("Could not find Discord destination: %s", channel_id)
                return False

            embed, view = self.format_discord_message(item, feed_title)
            await channel.send(embed=embed, view=view)
            return True
        except Exception as e:
            logger.error("Failed to send Discord notification to %s: %s", channel_id, e)
            return False

    # ---------------- Unified Dispatcher ----------------

    async def dispatch(
        self,
        platform: str,
        target_id: str,
        item: FeedItem,
        feed_title: str,
        thread_id: Optional[int] = None,
    ) -> bool:
        """Route notification to either Telegram or Discord based on platform string."""
        platform = platform.lower()
        if platform == "telegram":
            return await self.send_telegram(
                target_id, item, feed_title, thread_id=thread_id
            )
        elif platform == "discord":
            return await self.send_discord(target_id, item, feed_title)
        else:
            logger.warning("Unknown subscriber platform: %s", platform)
            return False
