import logging
import shlex
from typing import Optional, List, Tuple
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from database.db import Database
from parser.feed_parser import FeedParser, FeedItem, format_bytes, parse_size_bytes
from services.dispatcher import NotificationDispatcher
from services.poller import FeedPoller

try:
    from config import POLL_INTERVAL_SECONDS
except ImportError:
    from config_sample import POLL_INTERVAL_SECONDS

logger = logging.getLogger(__name__)


class DiscordBotService:
    """Discord bot interface for FeedPulse supporting both slash commands and prefix commands."""

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

        intents = discord.Intents.default()
        intents.message_content = True
        self.bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
        self._setup_events()
        self._setup_slash_commands()
        self._setup_prefix_commands()

    def _setup_events(self) -> None:
        """Register Discord bot event listeners."""

        @self.bot.event
        async def on_ready():
            self.dispatcher.set_discord_bot(self.bot)
            try:
                synced = await self.bot.tree.sync()
                logger.info("Synced %d Discord slash commands", len(synced))
            except Exception as e:
                logger.error("Failed to sync Discord slash commands: %s", e)

            logger.info(
                "Discord Bot logged in as %s (ID: %s) in %d guilds",
                self.bot.user,
                self.bot.user.id,
                len(self.bot.guilds),
            )

    @staticmethod
    def _parse_tokens(
        args: List[str],
    ) -> Tuple[
        Optional[str],
        Optional[str],
        Optional[str],
        Optional[str],
        bool,
        Optional[int],
        Optional[int],
    ]:
        url = None
        inf = None
        exf = None
        case_sensitive = False
        max_size = None
        min_size = None
        title_words = []

        rem = list(args)
        i = 0
        while i < len(rem):
            tok = rem[i].lower()
            if rem[i].startswith("http://") or rem[i].startswith("https://"):
                url = rem[i].strip("<>")
                i += 1
                continue
            elif tok in ("-inf", "--inf", "-inc", "--inc"):
                i += 1
                collected = []
                while i < len(rem) and not rem[i].startswith("-"):
                    collected.append(rem[i])
                    i += 1
                inf = " ".join(collected)
                continue
            elif tok.startswith(("inc=", "inf=")):
                inf = rem[i].split("=", 1)[1]
                i += 1
                continue
            elif tok in ("-exf", "--exf", "-exc", "--exc"):
                i += 1
                collected = []
                while i < len(rem) and not rem[i].startswith("-"):
                    collected.append(rem[i])
                    i += 1
                exf = " ".join(collected)
                continue
            elif tok.startswith(("exc=", "exf=")):
                exf = rem[i].split("=", 1)[1]
                i += 1
                continue
            elif tok in ("-stv", "--stv", "-cs", "--cs"):
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
            elif tok.startswith(("stv=", "cs=")):
                case_sensitive = rem[i].split("=", 1)[1].lower() in (
                    "true",
                    "1",
                    "yes",
                )
                i += 1
                continue
            elif tok in ("-maxsize", "--maxsize", "-max", "--max"):
                i += 1
                if i < len(rem):
                    max_size = parse_size_bytes(rem[i])
                    i += 1
                continue
            elif tok.startswith(("maxsize=", "max=")):
                max_size = parse_size_bytes(rem[i].split("=", 1)[1])
                i += 1
                continue
            elif tok in ("-minsize", "--minsize", "-min", "--min"):
                i += 1
                if i < len(rem):
                    min_size = parse_size_bytes(rem[i])
                    i += 1
                continue
            elif tok.startswith(("minsize=", "min=")):
                min_size = parse_size_bytes(rem[i].split("=", 1)[1])
                i += 1
                continue
            elif not url:
                title_words.append(rem[i])
            i += 1

        custom_title = " ".join(title_words).strip() or None
        return url, custom_title, inf, exf, case_sensitive, max_size, min_size

    def _setup_slash_commands(self) -> None:
        """Register Discord Slash Application Commands (/add, /list, etc.)."""

        @self.bot.tree.command(
            name="help", description="Show FeedPulse commands and help"
        )
        async def slash_help(interaction: discord.Interaction):
            embed = discord.Embed(
                title="⚡ FeedPulse - Torrent & RSS Bot",
                description=(
                    "I track RSS & Torrent feeds in real-time and post notifications "
                    "here as soon as new items or torrents are uploaded!\n\n"
                    "**Slash Commands:**\n"
                    "➕ `/add <url> [filter_include] [filter_exclude]` - Subscribe to a feed\n"
                    "📋 `/list` - List active feeds in this channel\n"
                    "🔍 `/get <feed> [count]` - Preview live feed items immediately\n"
                    "⏸️ `/pause <id_or_all>` - Pause notifications for a feed or all feeds\n"
                    "▶️ `/resume <id_or_all>` - Resume notifications for a feed or all feeds\n"
                    "✏️ `/edit <id> [title] [include] [exclude] [url]` - Edit a feed in-place\n"
                    "➖ `/remove <id_or_url>` - Unsubscribe from a feed\n"
                    "🔍 `/filter <id> <include> <exclude>` - Set keyword filters\n"
                    "🔄 `/check` - Force an immediate feed check\n"
                    "🧪 `/test` - Preview a sample torrent alert\n\n"
                    "*(Prefix commands like `!add <url>`, `!get <id>`, `!pause <id>`, `!resume <id>`, `!edit <id>` also work!)*"
                ),
                color=0x2ECC71,
            )
            embed.add_field(
                name="Example Feed",
                value="`/add url: https://example.com/rss/feed.xml`",
                inline=False,
            )
            await interaction.response.send_message(embed=embed)

        @self.bot.tree.command(
            name="add", description="Subscribe this channel to an RSS/Torrent feed"
        )
        @app_commands.describe(
            url="The RSS or Atom feed URL (e.g. https://example.com/rss/feed.xml)",
            filter_include="Optional comma-separated keywords that MUST appear in the title",
            filter_exclude="Optional comma-separated keywords to ignore",
            case_sensitive="Strict case sensitivity for keyword filters (default False)",
            max_size="Optional max file size limit (e.g. 5GB, 2000MB)",
            min_size="Optional min file size limit (e.g. 500MB, 1GB)",
        )
        async def slash_add(
            interaction: discord.Interaction,
            url: str,
            filter_include: Optional[str] = None,
            filter_exclude: Optional[str] = None,
            case_sensitive: bool = False,
            max_size: Optional[str] = None,
            min_size: Optional[str] = None,
        ):
            await interaction.response.defer(thinking=True)
            url = url.strip()
            if not (url.startswith("http://") or url.startswith("https://")):
                await interaction.followup.send(
                    "❌ Please provide a valid HTTP/HTTPS URL."
                )
                return

            max_sz_bytes = parse_size_bytes(max_size) if max_size else None
            min_sz_bytes = parse_size_bytes(min_size) if min_size else None

            try:
                async with aiohttp.ClientSession() as session:
                    (
                        title,
                        etag,
                        modified,
                        items,
                        status,
                    ) = await FeedParser.fetch_and_parse(session, url)

                if status != 200 or not items:
                    await interaction.followup.send(
                        f"❌ Could not load feed from `<{url}>` (HTTP Status: {status}).\n"
                        "Please verify the URL is valid and returns RSS XML."
                    )
                    return

                feed_title = title or url
                channel_id = str(interaction.channel_id)

                feed_id, effective_title, is_new = await self.db.subscribe(
                    platform="discord",
                    target_id=channel_id,
                    url=url,
                    title=feed_title,
                    filter_keywords=filter_include,
                    filter_exclude=filter_exclude,
                    case_sensitive=case_sensitive,
                    max_size=max_sz_bytes,
                    min_size=min_sz_bytes,
                )

                self.poller.trigger_immediate_check()

                embed = discord.Embed(
                    title="✅ Feed Subscribed Successfully!"
                    if is_new
                    else "ℹ️ Already Subscribed",
                    color=0x2ECC71 if is_new else 0xF1C40F,
                )
                embed.add_field(name="📡 Feed Name", value=effective_title, inline=True)
                embed.add_field(name="🆔 Feed ID", value=str(feed_id), inline=True)
                embed.add_field(
                    name="📦 Current Items", value=str(len(items)), inline=True
                )
                embed.add_field(name="🔗 URL", value=url, inline=False)

                if filter_include:
                    embed.add_field(
                        name="🔍 Required Keywords", value=filter_include, inline=True
                    )
                if filter_exclude:
                    embed.add_field(
                        name="🚫 Excluded Keywords", value=filter_exclude, inline=True
                    )
                if case_sensitive:
                    embed.add_field(
                        name="🔤 Case-Sensitive", value="Enabled", inline=True
                    )
                if min_sz_bytes:
                    embed.add_field(
                        name="📏 Min Size",
                        value=format_bytes(min_sz_bytes),
                        inline=True,
                    )
                if max_sz_bytes:
                    embed.add_field(
                        name="📏 Max Size",
                        value=format_bytes(max_sz_bytes),
                        inline=True,
                    )

                embed.set_footer(
                    text=f"Checking for new uploads every {POLL_INTERVAL_SECONDS} seconds."
                )
                await interaction.followup.send(embed=embed)

            except Exception as e:
                logger.error("Error in /add command: %s", e)
                await interaction.followup.send(f"❌ Error adding feed: {e}")

        @self.bot.tree.command(
            name="batchadd",
            description="Subscribe this channel to multiple RSS feeds at once (one per line)",
        )
        @app_commands.describe(
            feeds="Multiple feed subscriptions, one per line (e.g. Title https://... -inf ...)",
        )
        async def slash_batchadd(interaction: discord.Interaction, feeds: str):
            await interaction.response.defer(thinking=True)
            lines = [line.strip() for line in feeds.splitlines() if line.strip()]
            if not lines:
                await interaction.followup.send(
                    "⚠️ Please provide at least one feed line."
                )
                return

            results = []
            added_count = 0
            channel_id = str(interaction.channel_id)

            async with aiohttp.ClientSession() as session:
                for idx, line in enumerate(lines, 1):
                    try:
                        tokens = shlex.split(line)
                    except ValueError:
                        tokens = line.split()

                    (
                        url,
                        custom_title,
                        inf,
                        exf,
                        case_sensitive,
                        max_size,
                        min_size,
                    ) = self._parse_tokens(tokens)
                    if not url:
                        results.append(
                            f"❌ **#{idx}:** No valid URL found in `{line[:35]}...`"
                        )
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
                                f"❌ **#{idx}:** Failed to load feed (HTTP {status}) from `<{url[:30]}...>`"
                            )
                            continue

                        feed_title = custom_title or title or url
                        feed_id, effective_title, is_new = await self.db.subscribe(
                            platform="discord",
                            target_id=channel_id,
                            url=url,
                            title=feed_title,
                            filter_keywords=inf,
                            filter_exclude=exf,
                            case_sensitive=case_sensitive,
                            max_size=max_size,
                            min_size=min_size,
                        )
                        added_count += 1
                        status_icon = "✅" if is_new else "ℹ️"
                        results.append(
                            f"{status_icon} **#{idx}:** **{effective_title}** (`#{feed_id}`)"
                        )
                    except Exception as e:
                        results.append(f"❌ **#{idx}:** Error: {e}")

            if added_count > 0:
                self.poller.trigger_immediate_check()

            embed = discord.Embed(
                title=f"📦 Batch Subscription Results ({added_count}/{len(lines)} added)",
                description="\n".join(results[:25]),
                color=0x2ECC71 if added_count > 0 else 0xE74C3C,
            )
            await interaction.followup.send(embed=embed)

        @self.bot.tree.command(
            name="list", description="List feeds monitored in this channel"
        )
        async def slash_list(interaction: discord.Interaction):
            channel_id = str(interaction.channel_id)
            subs = await self.db.list_subscriptions("discord", channel_id)

            if not subs:
                await interaction.response.send_message(
                    "📭 **No active subscriptions in this channel.**\n"
                    "Use `/add <url>` to start monitoring a feed!"
                )
                return

            embed = discord.Embed(
                title=f"📋 Subscribed Feeds in #{interaction.channel} ({len(subs)})",
                color=0x3498DB,
            )
            for s in subs:
                fid = s["feed_id"]
                title = s["title"] or "Untitled Feed"
                url = s["url"]
                last_ch = s["last_checked"] or "Not yet checked"

                val = f"🔗 [Feed Link]({url})\n⏱ *Last check: {last_ch}*"
                if s.get("filter_keywords"):
                    val += f"\n🔍 *Include:* `{s['filter_keywords']}`"
                if s.get("filter_exclude"):
                    val += f"\n🚫 *Exclude:* `{s['filter_exclude']}`"
                if s.get("case_sensitive"):
                    val += "\n🔤 *Case-Sensitive:* `Enabled`"
                sz_items = []
                if s.get("min_size"):
                    sz_items.append(f"Min: {format_bytes(s['min_size'])}")
                if s.get("max_size"):
                    sz_items.append(f"Max: {format_bytes(s['max_size'])}")
                if sz_items:
                    val += f"\n📏 *Size Limit:* `{', '.join(sz_items)}`"

                paused_tag = " ⏸️ [PAUSED]" if s.get("is_paused") else ""
                embed.add_field(
                    name=f"[{fid}] {title[:180]}{paused_tag}",
                    value=val,
                    inline=False,
                )

            embed.set_footer(
                text="Manage: /pause <id> | /resume <id> | /edit <id> | /remove <id>"
            )
            await interaction.response.send_message(embed=embed)

        @self.bot.tree.command(
            name="pause",
            description="Pause notifications for a feed or all feeds in this channel",
        )
        @app_commands.describe(
            feed_id_or_all="The numeric feed ID (from /list) or 'all' to pause all feeds"
        )
        async def slash_pause(interaction: discord.Interaction, feed_id_or_all: str):
            channel_id = str(interaction.channel_id)
            feed_label, status, count = await self.db.set_subscription_pause(
                "discord", channel_id, feed_id_or_all, paused=True
            )
            if status == "success":
                if feed_id_or_all.lower() == "all":
                    await interaction.response.send_message(
                        f"⏸️ **Paused {count} feed subscription(s)** in this channel."
                    )
                else:
                    await interaction.response.send_message(
                        f"⏸️ **Feed Paused:** `{feed_label or feed_id_or_all}` has been paused."
                    )
            elif status == "already_paused":
                await interaction.response.send_message(
                    f"ℹ️ Feed `{feed_label or feed_id_or_all}` is already paused."
                )
            else:
                await interaction.response.send_message(
                    f"❌ No matching subscription found for `{feed_id_or_all}`.\n"
                    "Use `/list` to check feed IDs."
                )

        @self.bot.tree.command(
            name="resume",
            description="Resume notifications for a feed or all feeds in this channel",
        )
        @app_commands.describe(
            feed_id_or_all="The numeric feed ID (from /list) or 'all' to resume all feeds"
        )
        async def slash_resume(interaction: discord.Interaction, feed_id_or_all: str):
            channel_id = str(interaction.channel_id)
            feed_label, status, count = await self.db.set_subscription_pause(
                "discord", channel_id, feed_id_or_all, paused=False
            )
            if status == "success":
                if feed_id_or_all.lower() == "all":
                    await interaction.response.send_message(
                        f"▶️ **Resumed {count} feed subscription(s)** in this channel."
                    )
                else:
                    await interaction.response.send_message(
                        f"▶️ **Feed Resumed:** `{feed_label or feed_id_or_all}` is now active."
                    )
            elif status == "already_resumed":
                await interaction.response.send_message(
                    f"ℹ️ Feed `{feed_label or feed_id_or_all}` is already active."
                )
            else:
                await interaction.response.send_message(
                    f"❌ No matching subscription found for `{feed_id_or_all}`.\n"
                    "Use `/list` to check feed IDs."
                )

        @self.bot.tree.command(
            name="remove", description="Unsubscribe from a feed in this channel"
        )
        @app_commands.describe(
            feed_id_or_url="The numeric feed ID (from /list) or feed URL"
        )
        async def slash_remove(interaction: discord.Interaction, feed_id_or_url: str):
            channel_id = str(interaction.channel_id)
            removed, status = await self.db.unsubscribe(
                "discord", channel_id, feed_id_or_url
            )

            if status == "success":
                await interaction.response.send_message(
                    f"🗑 **Unsubscribed:** Removed `{removed or feed_id_or_url}` from this channel."
                )
            else:
                await interaction.response.send_message(
                    f"❌ Could not find an active subscription matching `{feed_id_or_url}`.\n"
                    "Use `/list` to check feed IDs."
                )

        @self.bot.tree.command(
            name="edit",
            description="Edit a feed subscription in this channel (title, filters, or URL)",
        )
        @app_commands.describe(
            feed_id="The numeric feed ID from /list",
            title="Optional new custom display title (or 'none' to reset)",
            filter_include="Optional keywords that MUST be in the title (or 'none' to clear)",
            filter_exclude="Optional keywords to ignore (or 'none' to clear)",
            new_url="Optional new feed RSS/Atom URL",
            case_sensitive="Toggle case sensitivity for filters (True/False)",
            max_size="Optional max file size limit (e.g. 5GB, 2000MB or 'none' to clear)",
            min_size="Optional min file size limit (e.g. 500MB, 1GB or 'none' to clear)",
        )
        async def slash_edit(
            interaction: discord.Interaction,
            feed_id: str,
            title: Optional[str] = None,
            filter_include: Optional[str] = None,
            filter_exclude: Optional[str] = None,
            new_url: Optional[str] = None,
            case_sensitive: Optional[bool] = None,
            max_size: Optional[str] = None,
            min_size: Optional[str] = None,
        ):
            channel_id = str(interaction.channel_id)
            if not any(
                x is not None
                for x in (
                    title,
                    filter_include,
                    filter_exclude,
                    new_url,
                    case_sensitive,
                    max_size,
                    min_size,
                )
            ):
                await interaction.response.send_message(
                    "⚠️ Please specify at least one option to edit: `title`, `filter_include`, `filter_exclude`, `new_url`, `case_sensitive`, `max_size`, or `min_size`."
                )
                return

            max_sz_val = (
                -1
                if max_size and max_size.lower() in ("none", "clear", "0")
                else parse_size_bytes(max_size)
                if max_size
                else None
            )
            min_sz_val = (
                -1
                if min_size and min_size.lower() in ("none", "clear", "0")
                else parse_size_bytes(min_size)
                if min_size
                else None
            )

            feed_label, status, changed = await self.db.edit_subscription(
                platform="discord",
                target_id=channel_id,
                feed_identifier=feed_id,
                title=title,
                filter_keywords=filter_include,
                filter_exclude=filter_exclude,
                new_url=new_url,
                case_sensitive=case_sensitive,
                max_size=max_sz_val,
                min_size=min_sz_val,
            )

            if status == "success":
                embed = discord.Embed(
                    title=f"✏️ Updated Feed #{feed_id}",
                    color=0x2ECC71,
                )
                embed.add_field(
                    name="📡 Feed",
                    value=feed_label or feed_id,
                    inline=False,
                )
                for k, v in changed.items():
                    embed.add_field(
                        name=f"• {k.replace('_', ' ').title()}",
                        value=f"`{v}`",
                        inline=True,
                    )
                await interaction.response.send_message(embed=embed)
                self.poller.trigger_immediate_check()
            elif status == "no_changes":
                await interaction.response.send_message(
                    f"ℹ️ No changes were made to `{feed_label or feed_id}`."
                )
            else:
                await interaction.response.send_message(
                    f"❌ No matching subscription found for `{feed_id}`."
                )

        @self.bot.tree.command(
            name="get",
            description="Inspect and preview live items from a feed right now",
        )
        @app_commands.describe(
            feed="Feed ID (from /list) or full feed URL",
            count="Number of latest items to preview (default: 5, max: 20)",
        )
        async def slash_get(
            interaction: discord.Interaction,
            feed: str,
            count: Optional[int] = 5,
        ):
            await interaction.response.defer(thinking=True)
            target_input = feed.strip()
            item_count = max(1, min(count or 5, 20))

            if target_input.startswith(("http://", "https://")):
                feed_url = target_input
                feed_label = target_input
            else:
                feed_doc = await self.db.get_feed_by_identifier(target_input)
                if not feed_doc:
                    await interaction.followup.send(
                        f"❌ **Feed not found!** Could not find a feed matching `{target_input}`.\n"
                        "Use `/list` to view your active feed IDs or provide a full URL."
                    )
                    return
                feed_url = feed_doc["url"]
                feed_label = feed_doc.get("title") or feed_url

            try:
                async with aiohttp.ClientSession() as session:
                    (
                        title,
                        etag,
                        modified,
                        items,
                        status,
                    ) = await FeedParser.fetch_and_parse(session, feed_url)

                if status != 200 or not items:
                    await interaction.followup.send(
                        f"❌ Failed to fetch feed (HTTP {status}). The feed may be offline or empty."
                    )
                    return

                preview_items = items[:item_count]
                feed_name = title or feed_label

                embed = discord.Embed(
                    title=f"📋 Feed Inspector: {feed_name[:200]}",
                    url=feed_url
                    if feed_url.startswith(("http://", "https://"))
                    else None,
                    description=f"Showing latest **{len(preview_items)}** of **{len(items)}** items",
                    color=0x5865F2,
                )

                raw_lines = [
                    f"Feed: {feed_name}",
                    f"URL: {feed_url}",
                    f"Items: {len(preview_items)}/{len(items)}",
                    "=" * 40,
                    "",
                ]

                total_field_chars = 0
                for idx, item in enumerate(preview_items, 1):
                    item_title = item.title.strip()
                    val_parts = []
                    if item.size_str:
                        val_parts.append(f"📏 **Size:** {item.size_str}")
                    if item.seeds is not None:
                        val_parts.append(
                            f"🌱 **Seeds:** {item.seeds} | **Leechers:** {item.leechers or 0}"
                        )
                    if item.published:
                        val_parts.append(f"📅 **Date:** {item.published}")

                    links = []
                    if item.link:
                        links.append(f"[Web Page]({item.link})")
                    if item.torrent_url:
                        links.append(f"[Torrent File]({item.torrent_url})")
                    if links:
                        val_parts.append("🔗 " + " | ".join(links))

                    val_str = "\n".join(val_parts) or "No additional metadata"
                    if len(val_str) > 1024:
                        val_str = val_str[:1020] + "..."

                    field_name = f"#{idx} {item_title}"
                    if len(field_name) > 256:
                        field_name = field_name[:252] + "..."

                    total_field_chars += len(field_name) + len(val_str)
                    if total_field_chars < 5500 and len(embed.fields) < 25:
                        embed.add_field(name=field_name, value=val_str, inline=False)

                    raw_lines.append(f"#{idx} {item_title}")
                    if item.size_str:
                        raw_lines.append(f"Size: {item.size_str}")
                    if item.seeds is not None:
                        raw_lines.append(
                            f"Seeds: {item.seeds} | Leechers: {item.leechers}"
                        )
                    if item.published:
                        raw_lines.append(f"Date: {item.published}")
                    if item.link:
                        raw_lines.append(f"Link: {item.link}")
                    if item.magnet_uri:
                        raw_lines.append(f"Magnet: {item.magnet_uri}")
                    raw_lines.append("-" * 30)

                await interaction.followup.send(embed=embed)
            except Exception as e:
                logger.error("Discord feed inspector error for %s: %s", feed_url, e)
                await interaction.followup.send(f"❌ Error inspecting feed: {e}")

        @self.bot.tree.command(
            name="check", description="Force an immediate RSS feed check"
        )
        async def slash_check(interaction: discord.Interaction):
            self.poller.trigger_immediate_check()
            await interaction.response.send_message(
                "🔄 **Immediate feed check triggered!** Scanning feeds now..."
            )

        @self.bot.tree.command(
            name="test", description="Send a preview torrent notification"
        )
        async def slash_test(interaction: discord.Interaction):
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
            embed, view = self.dispatcher.format_discord_message(
                sample_item, "Sample RSS Feed (Preview)"
            )
            await interaction.response.send_message(embed=embed, view=view)

        @self.bot.tree.command(
            name="authorize", description="Authorize a channel or user (Admin only)"
        )
        @app_commands.describe(
            target_id="Channel ID or User ID to authorize (default: current channel)",
            id_type="Type of target ('chat' or 'user', default: chat)",
        )
        async def slash_authorize(
            interaction: discord.Interaction,
            target_id: Optional[str] = None,
            id_type: str = "chat",
        ):
            perms = getattr(interaction.user, "guild_permissions", None)
            is_admin = perms and (perms.administrator or perms.manage_guild)
            if not is_admin:
                await interaction.response.send_message(
                    "❌ **Permission Denied:** Administrator or Manage Server permission required.",
                    ephemeral=True,
                )
                return

            tid = (
                int(target_id)
                if target_id and target_id.lstrip("-").isdigit()
                else interaction.channel_id
            )
            target_title = (
                interaction.channel.name
                if not target_id and hasattr(interaction.channel, "name")
                else str(tid)
            )

            await self.db.add_authorization(
                target_id=tid,
                id_type=id_type,
                title=target_title,
                added_by=interaction.user.id,
            )
            embed = discord.Embed(
                title="✅ Successfully Authorized",
                description=f"**Target ID:** `{tid}` ({id_type.capitalize()})\n**Title:** {target_title}",
                color=0x2ECC71,
            )
            await interaction.response.send_message(embed=embed)

        @self.bot.tree.command(
            name="unauthorize",
            description="Revoke authorization for a channel or user (Admin only)",
        )
        @app_commands.describe(
            target_id="Channel ID or User ID to revoke (default: current channel)",
        )
        async def slash_unauthorize(
            interaction: discord.Interaction,
            target_id: Optional[str] = None,
        ):
            perms = getattr(interaction.user, "guild_permissions", None)
            is_admin = perms and (perms.administrator or perms.manage_guild)
            if not is_admin:
                await interaction.response.send_message(
                    "❌ **Permission Denied:** Administrator or Manage Server permission required.",
                    ephemeral=True,
                )
                return

            tid = (
                int(target_id)
                if target_id and target_id.lstrip("-").isdigit()
                else interaction.channel_id
            )
            await self.db.remove_authorization(target_id=tid)
            embed = discord.Embed(
                title="🚫 Authorization Revoked",
                description=f"**Target ID:** `{tid}` is no longer authorized.",
                color=0xE74C3C,
            )
            await interaction.response.send_message(embed=embed)

        @self.bot.tree.command(
            name="authlist", description="View all authorized channels and users"
        )
        async def slash_authlist(interaction: discord.Interaction):
            auths = await self.db.get_all_authorizations()
            embed = discord.Embed(title="🛡️ Authorized Entities", color=0x3498DB)
            if not auths:
                embed.description = (
                    "*No dynamic authorizations configured in database.*"
                )
            else:
                desc_lines = []
                for a in auths:
                    tid = a.get("target_id")
                    itype = a.get("id_type", "chat")
                    name = a.get("title") or "N/A"
                    desc_lines.append(f"• `{tid}` ({itype.capitalize()}) — {name}")
                embed.description = "\n".join(desc_lines)
            await interaction.response.send_message(embed=embed)

    def _setup_prefix_commands(self) -> None:
        """Register traditional prefix commands (!add, !list, etc.) for convenience."""

        @self.bot.command(name="help")
        async def prefix_help(ctx: commands.Context):
            await ctx.send(
                "⚡ Use `/help` for interactive slash commands or `!add <url>` to subscribe."
            )

        @self.bot.command(name="add", aliases=["batchadd"])
        async def prefix_add(ctx: commands.Context, *args):
            raw_text = ctx.message.content
            lines = [line.strip() for line in raw_text.splitlines() if line.strip()]

            if lines and (
                lines[0].lower().startswith("!add")
                or lines[0].lower().startswith("!batchadd")
            ):
                first_parts = lines[0].split(maxsplit=1)
                if len(first_parts) > 1:
                    lines[0] = first_parts[1].strip()
                else:
                    lines.pop(0)

            if not lines:
                await ctx.send(
                    "⚠️ Usage: `!add <feed_url> [-inf <include>] [-exf <exclude>] [-maxsize <size>] [-minsize <size>] [-stv]`\n"
                    "Or multi-line for batch addition!"
                )
                return

            channel_id = str(ctx.channel.id)

            if len(lines) > 1:
                status_msg = await ctx.send(
                    f"⏳ *Processing batch subscription of {len(lines)} feeds...*"
                )
                results = []
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
                            inf,
                            exf,
                            case_sensitive,
                            max_size,
                            min_size,
                        ) = self._parse_tokens(tokens)

                        if not url:
                            results.append(
                                f"❌ **#{idx}:** No valid URL found in `{line[:35]}...`"
                            )
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
                                    f"❌ **#{idx}:** Failed to load feed (HTTP {status}) from `<{url[:30]}...>`"
                                )
                                continue

                            feed_title = custom_title or title or url
                            feed_id, effective_title, is_new = await self.db.subscribe(
                                platform="discord",
                                target_id=channel_id,
                                url=url,
                                title=feed_title,
                                filter_keywords=inf,
                                filter_exclude=exf,
                                case_sensitive=case_sensitive,
                                max_size=max_size,
                                min_size=min_size,
                            )
                            added_count += 1
                            status_icon = "✅" if is_new else "ℹ️"
                            results.append(
                                f"{status_icon} **#{idx}:** **{effective_title}** (`#{feed_id}`)"
                            )
                        except Exception as e:
                            results.append(f"❌ **#{idx}:** Error: {e}")

                if added_count > 0:
                    self.poller.trigger_immediate_check()

                embed = discord.Embed(
                    title=f"📦 Batch Subscription Results ({added_count}/{len(lines)} added)",
                    description="\n".join(results[:25]),
                    color=0x2ECC71 if added_count > 0 else 0xE74C3C,
                )
                await status_msg.edit(content="", embed=embed)
                return

            # Single feed parsing
            try:
                tokens = shlex.split(lines[0])
            except ValueError:
                tokens = lines[0].split()

            (
                url,
                custom_title,
                inf,
                exf,
                case_sensitive,
                max_size,
                min_size,
            ) = self._parse_tokens(tokens)

            if not url:
                await ctx.send("❌ Please provide a valid HTTP/HTTPS URL.")
                return

            try:
                async with aiohttp.ClientSession() as session:
                    (
                        title,
                        etag,
                        modified,
                        items,
                        status,
                    ) = await FeedParser.fetch_and_parse(session, url)

                if status != 200 or not items:
                    await ctx.send(
                        f"❌ Failed to fetch feed from `<{url}>` (HTTP Status: {status})."
                    )
                    return

                feed_title = custom_title or title or url
                feed_id, effective_title, is_new = await self.db.subscribe(
                    platform="discord",
                    target_id=channel_id,
                    url=url,
                    title=feed_title,
                    filter_keywords=inf,
                    filter_exclude=exf,
                    case_sensitive=case_sensitive,
                    max_size=max_size,
                    min_size=min_size,
                )
                self.poller.trigger_immediate_check()
                status_str = "Subscribed" if is_new else "Already subscribed to"
                msg = f"✅ **{status_str}** `{effective_title}` (ID: {feed_id})! Found {len(items)} items."
                if inf:
                    msg += f"\n🔍 Required: `{inf}`"
                if exf:
                    msg += f"\n🚫 Excluded: `{exf}`"
                if case_sensitive:
                    msg += "\n🔤 Case-Sensitive: `Enabled`"
                if min_size:
                    msg += f"\n📏 Min Size: `{format_bytes(min_size)}`"
                if max_size:
                    msg += f"\n📏 Max Size: `{format_bytes(max_size)}`"
                await ctx.send(msg)
            except Exception as e:
                await ctx.send(f"❌ Error: {e}")

        @self.bot.command(name="list")
        async def prefix_list(ctx: commands.Context):
            channel_id = str(ctx.channel.id)
            subs = await self.db.list_subscriptions("discord", channel_id)
            if not subs:
                await ctx.send("📭 No feeds tracked in this channel.")
                return
            msg = f"📋 **Feeds in #{ctx.channel}:**\n"
            for s in subs:
                paused_tag = " ⏸️ [PAUSED]" if s.get("is_paused") else ""
                cs_tag = " [Case-Sensitive]" if s.get("case_sensitive") else ""
                sz_tag = (
                    f" [Size: {format_bytes(s.get('min_size')) or '0 B'} - {format_bytes(s.get('max_size')) or 'Any'}]"
                    if (s.get("min_size") or s.get("max_size"))
                    else ""
                )
                msg += f"• **[{s['feed_id']}]** {s['title']}{paused_tag}{cs_tag}{sz_tag} - `<{s['url']}>\n`"
            msg += "\n*Manage:* `!pause <id>` | `!resume <id>` | `!edit <id>` | `!remove <id>`"
            await ctx.send(msg)

        @self.bot.command(name="pause")
        async def prefix_pause(ctx: commands.Context, feed_id_or_all: str = None):
            if not feed_id_or_all:
                await ctx.send("⚠️ Usage: `!pause <feed_id_or_all>`")
                return
            channel_id = str(ctx.channel.id)
            feed_label, status, count = await self.db.set_subscription_pause(
                "discord", channel_id, feed_id_or_all, paused=True
            )
            if status == "success":
                if feed_id_or_all.lower() == "all":
                    await ctx.send(f"⏸️ Paused {count} feed(s) in this channel.")
                else:
                    await ctx.send(
                        f"⏸️ Paused `{feed_label or feed_id_or_all}` in this channel."
                    )
            elif status == "already_paused":
                await ctx.send(f"ℹ️ `{feed_label or feed_id_or_all}` is already paused.")
            else:
                await ctx.send(
                    f"❌ No matching feed found for `{feed_id_or_all}` in this channel."
                )

        @self.bot.command(name="resume")
        async def prefix_resume(ctx: commands.Context, feed_id_or_all: str = None):
            if not feed_id_or_all:
                await ctx.send("⚠️ Usage: `!resume <feed_id_or_all>`")
                return
            channel_id = str(ctx.channel.id)
            feed_label, status, count = await self.db.set_subscription_pause(
                "discord", channel_id, feed_id_or_all, paused=False
            )
            if status == "success":
                if feed_id_or_all.lower() == "all":
                    await ctx.send(f"▶️ Resumed {count} feed(s) in this channel.")
                else:
                    await ctx.send(
                        f"▶️ Resumed `{feed_label or feed_id_or_all}` in this channel."
                    )
            elif status == "already_resumed":
                await ctx.send(f"ℹ️ `{feed_label or feed_id_or_all}` is already active.")
            else:
                await ctx.send(
                    f"❌ No matching feed found for `{feed_id_or_all}` in this channel."
                )

        @self.bot.command(name="remove", aliases=["del", "delete"])
        async def prefix_remove(ctx: commands.Context, feed_id: str = None):
            if not feed_id:
                await ctx.send("⚠️ Usage: `!remove <feed_id_or_url>`")
                return
            channel_id = str(ctx.channel.id)
            removed, status = await self.db.unsubscribe("discord", channel_id, feed_id)
            if status == "success":
                await ctx.send(f"🗑 Removed `{removed or feed_id}` from this channel.")
            else:
                await ctx.send(f"❌ No matching feed found for `{feed_id}`.")

        @self.bot.command(name="edit")
        async def prefix_edit(
            ctx: commands.Context,
            feed_id: str = None,
            *args,
        ):
            if not feed_id or not args:
                await ctx.send(
                    "⚠️ Usage: `!edit <feed_id> [-title <name>] [-inf <include>] [-exf <exclude>] [-url <new_url>] [-maxsize <size>] [-minsize <size>] [-stv]`"
                )
                return

            channel_id = str(ctx.channel.id)
            title = None
            inf = None
            exf = None
            new_url = None
            case_sensitive = None
            max_size = None
            min_size = None

            rem = list(args)
            i = 0
            while i < len(rem):
                tok = rem[i].lower()
                if tok in ("-title", "--title", "-name", "--name"):
                    i += 1
                    col = []
                    while i < len(rem) and not rem[i].startswith("-"):
                        col.append(rem[i])
                        i += 1
                    if col:
                        title = " ".join(col)
                    continue
                elif tok.startswith("title="):
                    title = rem[i].split("=", 1)[1]
                    i += 1
                    continue
                elif tok in ("-inf", "--inf", "-inc", "--inc"):
                    i += 1
                    col = []
                    while i < len(rem) and not rem[i].startswith("-"):
                        col.append(rem[i])
                        i += 1
                    if col:
                        inf = " ".join(col)
                    continue
                elif tok.startswith(("inc=", "inf=")):
                    inf = rem[i].split("=", 1)[1]
                    i += 1
                    continue
                elif tok in ("-exf", "--exf", "-exc", "--exc"):
                    i += 1
                    col = []
                    while i < len(rem) and not rem[i].startswith("-"):
                        col.append(rem[i])
                        i += 1
                    if col:
                        exf = " ".join(col)
                    continue
                elif tok.startswith(("exc=", "exf=")):
                    exf = rem[i].split("=", 1)[1]
                    i += 1
                    continue
                elif tok in ("-url", "--url", "-link", "--link"):
                    i += 1
                    if i < len(rem):
                        new_url = rem[i]
                        i += 1
                    continue
                elif tok.startswith(("url=", "link=")):
                    new_url = rem[i].split("=", 1)[1]
                    i += 1
                    continue
                elif tok in ("-stv", "--stv", "-cs", "--cs"):
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
                elif tok.startswith(("stv=", "cs=")):
                    case_sensitive = rem[i].split("=", 1)[1].lower() in (
                        "true",
                        "1",
                        "yes",
                    )
                    i += 1
                    continue
                elif tok in ("-maxsize", "--maxsize", "-max", "--max"):
                    i += 1
                    if i < len(rem):
                        val = rem[i].lower()
                        max_size = (
                            -1
                            if val in ("none", "clear", "0")
                            else parse_size_bytes(rem[i])
                        )
                        i += 1
                    continue
                elif tok.startswith(("maxsize=", "max=")):
                    val = rem[i].split("=", 1)[1].lower()
                    max_size = (
                        -1 if val in ("none", "clear", "0") else parse_size_bytes(val)
                    )
                    i += 1
                    continue
                elif tok in ("-minsize", "--minsize", "-min", "--min"):
                    i += 1
                    if i < len(rem):
                        val = rem[i].lower()
                        min_size = (
                            -1
                            if val in ("none", "clear", "0")
                            else parse_size_bytes(rem[i])
                        )
                        i += 1
                    continue
                elif tok.startswith(("minsize=", "min=")):
                    val = rem[i].split("=", 1)[1].lower()
                    min_size = (
                        -1 if val in ("none", "clear", "0") else parse_size_bytes(val)
                    )
                    i += 1
                    continue
                i += 1

            if not any(
                x is not None
                for x in (
                    title,
                    inf,
                    exf,
                    new_url,
                    case_sensitive,
                    max_size,
                    min_size,
                )
            ):
                await ctx.send(
                    "⚠️ Please specify at least one option to edit: `-title`, `-inf`, `-exf`, `-url`, `-stv`, `-maxsize`, or `-minsize`."
                )
                return

            feed_label, status, changed = await self.db.edit_subscription(
                platform="discord",
                target_id=channel_id,
                feed_identifier=feed_id,
                title=title,
                filter_keywords=inf,
                filter_exclude=exf,
                new_url=new_url,
                case_sensitive=case_sensitive,
                max_size=max_size,
                min_size=min_size,
            )

            if status == "success":
                lines = [f"✏️ **Updated Feed #{feed_id}** (`{feed_label or feed_id}`):"]
                for k, v in changed.items():
                    lines.append(f"• **{k.replace('_', ' ').title()}:** `{v}`")
                await ctx.send("\n".join(lines))
                self.poller.trigger_immediate_check()
            elif status == "no_changes":
                await ctx.send(f"ℹ️ No changes made to `{feed_label or feed_id}`.")
            else:
                await ctx.send(f"❌ Feed `{feed_id}` not found.")

        @self.bot.command(name="get", aliases=["preview", "inspect"])
        async def prefix_get(
            ctx: commands.Context, feed: str, count: Optional[int] = 5
        ):
            target_input = feed.strip()
            item_count = max(1, min(count or 5, 20))

            if target_input.startswith(("http://", "https://")):
                feed_url = target_input
                feed_label = target_input
            else:
                feed_doc = await self.db.get_feed_by_identifier(target_input)
                if not feed_doc:
                    await ctx.send(
                        f"❌ **Feed not found!** Could not find a feed matching `{target_input}`.\n"
                        "Use `!list` to view your active feed IDs or provide a full URL."
                    )
                    return
                feed_url = feed_doc["url"]
                feed_label = feed_doc.get("title") or feed_url

            status_msg = await ctx.send(f"⏳ *Inspecting live feed `{feed_label}`...*")

            try:
                async with aiohttp.ClientSession() as session:
                    (
                        title,
                        etag,
                        modified,
                        items,
                        status,
                    ) = await FeedParser.fetch_and_parse(session, feed_url)

                if status != 200 or not items:
                    await status_msg.edit(
                        content=f"❌ Failed to fetch feed (HTTP {status}). The feed may be offline or empty."
                    )
                    return

                preview_items = items[:item_count]
                feed_name = title or feed_label

                embed = discord.Embed(
                    title=f"📋 Feed Inspector: {feed_name[:200]}",
                    url=feed_url
                    if feed_url.startswith(("http://", "https://"))
                    else None,
                    description=f"Showing latest **{len(preview_items)}** of **{len(items)}** items",
                    color=0x5865F2,
                )

                total_field_chars = 0
                for idx, item in enumerate(preview_items, 1):
                    item_title = item.title.strip()
                    val_parts = []
                    if item.size_str:
                        val_parts.append(f"📏 **Size:** {item.size_str}")
                    if item.seeds is not None:
                        val_parts.append(
                            f"🌱 **Seeds:** {item.seeds} | **Leechers:** {item.leechers or 0}"
                        )
                    if item.published:
                        val_parts.append(f"📅 **Date:** {item.published}")

                    links = []
                    if item.link:
                        links.append(f"[Web Page]({item.link})")
                    if item.torrent_url:
                        links.append(f"[Torrent File]({item.torrent_url})")
                    if links:
                        val_parts.append("🔗 " + " | ".join(links))

                    val_str = "\n".join(val_parts) or "No additional metadata"
                    if len(val_str) > 1024:
                        val_str = val_str[:1020] + "..."

                    field_name = f"#{idx} {item_title}"
                    if len(field_name) > 256:
                        field_name = field_name[:252] + "..."

                    total_field_chars += len(field_name) + len(val_str)
                    if total_field_chars < 5500 and len(embed.fields) < 25:
                        embed.add_field(name=field_name, value=val_str, inline=False)

                await status_msg.edit(content="", embed=embed)
            except Exception as e:
                logger.error(
                    "Discord prefix get inspector error for %s: %s", feed_url, e
                )
                await status_msg.edit(content=f"❌ Error inspecting feed: {e}")

        @self.bot.command(name="check")
        async def prefix_check(ctx: commands.Context):
            self.poller.trigger_immediate_check()
            await ctx.send("🔄 Immediate check triggered!")

        @self.bot.command(name="test")
        async def prefix_test(ctx: commands.Context):
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
            embed, view = self.dispatcher.format_discord_message(
                sample_item, "Sample RSS Feed (Preview)"
            )
            await ctx.send(embed=embed, view=view)

        @self.bot.command(name="authorize", aliases=["a"])
        async def prefix_authorize(ctx: commands.Context, target: Optional[str] = None):
            perms = getattr(ctx.author, "guild_permissions", None)
            if not perms or (not perms.administrator and not perms.manage_guild):
                await ctx.send(
                    "❌ **Permission Denied:** Administrator permission required."
                )
                return

            tid = (
                int(target)
                if target and target.lstrip("-").isdigit()
                else ctx.channel.id
            )
            title = (
                ctx.channel.name
                if not target and hasattr(ctx.channel, "name")
                else str(tid)
            )
            await self.db.add_authorization(
                target_id=tid, id_type="chat", title=title, added_by=ctx.author.id
            )
            await ctx.send(f"✅ **Authorized:** `{tid}` ({title})")

        @self.bot.command(name="unauthorize", aliases=["ua"])
        async def prefix_unauthorize(
            ctx: commands.Context, target: Optional[str] = None
        ):
            perms = getattr(ctx.author, "guild_permissions", None)
            if not perms or (not perms.administrator and not perms.manage_guild):
                await ctx.send(
                    "❌ **Permission Denied:** Administrator permission required."
                )
                return

            tid = (
                int(target)
                if target and target.lstrip("-").isdigit()
                else ctx.channel.id
            )
            await self.db.remove_authorization(target_id=tid)
            await ctx.send(f"🚫 **Unauthorized:** `{tid}`")

        @self.bot.command(name="authlist")
        async def prefix_authlist(ctx: commands.Context):
            auths = await self.db.get_all_authorizations()
            if not auths:
                await ctx.send("🛡️ No dynamic authorizations found in database.")
                return
            lines = ["🛡️ **Authorized Entities:**"]
            for a in auths:
                lines.append(
                    f"• `{a.get('target_id')}` ({a.get('id_type', 'chat').capitalize()}) — {a.get('title', 'N/A')}"
                )
            await ctx.send("\n".join(lines))

    async def start(self) -> None:
        """Start the Discord bot client."""
        if not self.token:
            logger.warning("Discord bot token not provided; Discord bot is disabled.")
            return
        logger.info("Connecting to Discord...")
        await self.bot.start(self.token)

    async def stop(self) -> None:
        """Gracefully close the Discord connection."""
        if not self.bot.is_closed():
            logger.info("Closing Discord Bot connection...")
            await self.bot.close()
            logger.info("Discord Bot closed")
