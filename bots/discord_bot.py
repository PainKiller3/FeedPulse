import logging
from typing import Optional
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from database.db import Database
from parser.feed_parser import FeedParser, FeedItem
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
                    "⏸️ `/pause <id_or_all>` - Pause notifications for a feed or all feeds\n"
                    "▶️ `/resume <id_or_all>` - Resume notifications for a feed or all feeds\n"
                    "✏️ `/edit <id> [title] [include] [exclude] [url]` - Edit a feed in-place\n"
                    "➖ `/remove <id_or_url>` - Unsubscribe from a feed\n"
                    "🔍 `/filter <id> <include> <exclude>` - Set keyword filters\n"
                    "🔄 `/check` - Force an immediate feed check\n"
                    "🧪 `/test` - Preview a sample torrent alert\n\n"
                    "*(Prefix commands like `!add <url>`, `!pause <id>`, `!resume <id>`, `!edit <id>` also work!)*"
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
        )
        async def slash_add(
            interaction: discord.Interaction,
            url: str,
            filter_include: Optional[str] = None,
            filter_exclude: Optional[str] = None,
            case_sensitive: bool = False,
        ):
            await interaction.response.defer(thinking=True)
            url = url.strip()
            if not (url.startswith("http://") or url.startswith("https://")):
                await interaction.followup.send(
                    "❌ Please provide a valid HTTP/HTTPS URL."
                )
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

                embed.set_footer(
                    text=f"Checking for new uploads every {POLL_INTERVAL_SECONDS} seconds."
                )
                await interaction.followup.send(embed=embed)

            except Exception as e:
                logger.error("Error in /add command: %s", e)
                await interaction.followup.send(f"❌ Error adding feed: {e}")

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
        )
        async def slash_edit(
            interaction: discord.Interaction,
            feed_id: str,
            title: Optional[str] = None,
            filter_include: Optional[str] = None,
            filter_exclude: Optional[str] = None,
            new_url: Optional[str] = None,
            case_sensitive: Optional[bool] = None,
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
                )
            ):
                await interaction.response.send_message(
                    "⚠️ Please specify at least one option to edit: `title`, `filter_include`, `filter_exclude`, `new_url`, or `case_sensitive`."
                )
                return

            feed_label, status, changed = await self.db.edit_subscription(
                platform="discord",
                target_id=channel_id,
                feed_identifier=feed_id,
                title=title,
                filter_keywords=filter_include,
                filter_exclude=filter_exclude,
                new_url=new_url,
                case_sensitive=case_sensitive,
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

    def _setup_prefix_commands(self) -> None:
        """Register traditional prefix commands (!add, !list, etc.) for convenience."""

        @self.bot.command(name="help")
        async def prefix_help(ctx: commands.Context):
            await ctx.send(
                "⚡ Use `/help` for interactive slash commands or `!add <url>` to subscribe."
            )

        @self.bot.command(name="add")
        async def prefix_add(ctx: commands.Context, *args):
            if not args:
                await ctx.send(
                    "⚠️ Usage: `!add <feed_url> [-inf <include>] [-exf <exclude>] [-stv]`"
                )
                return

            url = None
            inf = None
            exf = None
            case_sensitive = False

            rem = list(args)
            for idx, a in enumerate(rem):
                if a.startswith("http://") or a.startswith("https://"):
                    url = a
                    rem = rem[:idx] + rem[idx + 1 :]
                    break

            if not url:
                await ctx.send("❌ Please provide a valid HTTP/HTTPS URL.")
                return

            i = 0
            while i < len(rem):
                tok = rem[i].lower()
                if tok in ("-inf", "--inf", "-inc", "--inc"):
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
                i += 1

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

                channel_id = str(ctx.channel.id)
                feed_id, effective_title, is_new = await self.db.subscribe(
                    platform="discord",
                    target_id=channel_id,
                    url=url,
                    title=title or url,
                    filter_keywords=inf,
                    filter_exclude=exf,
                    case_sensitive=case_sensitive,
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
                msg += f"• **[{s['feed_id']}]** {s['title']}{paused_tag}{cs_tag} - `<{s['url']}>\n`"
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
                    "⚠️ Usage: `!edit <feed_id> [-title <name>] [-inf <include>] [-exf <exclude>] [-url <new_url>] [-stv]`"
                )
                return

            channel_id = str(ctx.channel.id)
            title = None
            inf = None
            exf = None
            new_url = None
            case_sensitive = None

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
                i += 1

            if not any(
                x is not None for x in (title, inf, exf, new_url, case_sensitive)
            ):
                await ctx.send(
                    "⚠️ Please specify at least one option to edit: `-title`, `-inf`, `-exf`, `-url`, or `-stv`."
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
