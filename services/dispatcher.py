import html
import logging
from typing import Any, Optional
from parser.feed_parser import FeedItem

logger = logging.getLogger(__name__)


class NotificationDispatcher:
    """Dispatches formatted RSS notifications to Telegram and Discord subscribers."""

    def __init__(self):
        self.telegram_bot = None  # telegram.Bot instance
        self.discord_bot = None  # discord.ext.commands.Bot instance

    def set_telegram_bot(self, bot: Any) -> None:
        self.telegram_bot = bot

    def set_discord_bot(self, bot: Any) -> None:
        self.discord_bot = bot

    # ---------------- Telegram Formatting & Sending ----------------

    def format_telegram_message(self, item: FeedItem, feed_title: str) -> tuple:
        """Create HTML message and inline keyboard for Telegram."""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        safe_title = html.escape(item.title)
        safe_feed = html.escape(feed_title)

        if item.is_torrent:
            lines = [
                "⚡ <b>New Torrent Uploaded!</b>",
                f"📡 <i>{safe_feed}</i>\n",
                f"🎬 <b>{safe_title}</b>\n",
            ]

            meta_parts = []
            if item.category:
                meta_parts.append(f"🏷 <b>Category:</b> {html.escape(item.category)}")
            if item.size_str:
                meta_parts.append(f"📦 <b>Size:</b> {html.escape(item.size_str)}")
            if item.seeds is not None or item.leechers is not None:
                s = item.seeds if item.seeds is not None else "?"
                leech = item.leechers if item.leechers is not None else "?"
                meta_parts.append(f"🟢 <b>Seeds:</b> {s} | 🔴 <b>Leechers:</b> {leech}")

            if meta_parts:
                lines.append("\n".join(meta_parts) + "\n")

            if item.magnet_uri:
                lines.append("🧲 <b>Magnet URI:</b> <i>(tap to copy)</i>")
                lines.append(f"<code>{html.escape(item.magnet_uri)}</code>\n")

            # Inline keyboard buttons
            buttons = []
            row = []
            if item.torrent_url and item.torrent_url.startswith(
                ("http://", "https://")
            ):
                row.append(
                    InlineKeyboardButton("📥 Download .torrent", url=item.torrent_url)
                )
            if item.link and item.link.startswith(("http://", "https://")):
                row.append(InlineKeyboardButton("🌐 View Page", url=item.link))

            if row:
                buttons.append(row)

            keyboard = InlineKeyboardMarkup(buttons) if buttons else None
            return "\n".join(lines).strip(), keyboard

        else:
            # Regular RSS feed (article / blog post / news)
            lines = [
                "📰 <b>New Feed Post:</b>",
                f"📡 <i>{safe_feed}</i>\n",
                f"📝 <b>{safe_title}</b>\n",
            ]
            if item.published:
                lines.append(f"📅 <i>{html.escape(item.published)}</i>\n")

            buttons = []
            if item.link and item.link.startswith(("http://", "https://")):
                buttons.append([InlineKeyboardButton("🔗 Read Article", url=item.link)])

            keyboard = InlineKeyboardMarkup(buttons) if buttons else None
            return "\n".join(lines).strip(), keyboard

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

        try:
            text, reply_markup = self.format_telegram_message(item, feed_title)
            send_kwargs = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "reply_markup": reply_markup,
                "disable_web_page_preview": False,
            }
            if thread_id is not None:
                send_kwargs["message_thread_id"] = int(thread_id)

            await self.telegram_bot.send_message(**send_kwargs)
            return True
        except Exception as e:
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
