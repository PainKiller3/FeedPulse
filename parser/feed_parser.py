import json
import re
import html
import logging
import hashlib
import urllib.parse
from dataclasses import dataclass
from typing import Optional, List, Tuple, Any
import aiohttp
import feedparser

try:
    from config import USER_AGENT, DEFAULT_TRACKERS
except ImportError:
    from config_sample import USER_AGENT, DEFAULT_TRACKERS

logger = logging.getLogger(__name__)


def format_bytes(size_bytes: Optional[int]) -> Optional[str]:
    """Format bytes into a human-readable string (e.g. 512.4 MB, 1.82 GB)."""
    if size_bytes is None or size_bytes <= 0:
        return None
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    size = float(size_bytes)
    for unit in units:
        if size < 1024.0 or unit == "PB":
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size_bytes} B"


def parse_size_bytes(size_val: Any) -> Optional[int]:
    """
    Parse a byte count or human-readable size string (e.g. '1.5 GB', '700MB', '2500000') into integer bytes.
    """
    if size_val is None:
        return None
    if isinstance(size_val, (int, float)):
        return int(size_val)
    s = str(size_val).strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)

    match = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*([a-zA-Z]+)?$", s)
    if not match:
        return None
    number = float(match.group(1))
    unit = (match.group(2) or "b").upper()

    multipliers = {
        "B": 1,
        "BYTES": 1,
        "KB": 1024,
        "KIB": 1024,
        "K": 1024,
        "MB": 1024**2,
        "MIB": 1024**2,
        "M": 1024**2,
        "GB": 1024**3,
        "GIB": 1024**3,
        "G": 1024**3,
        "TB": 1024**4,
        "TIB": 1024**4,
        "T": 1024**4,
        "PB": 1024**5,
        "PIB": 1024**5,
        "P": 1024**5,
    }
    mult = multipliers.get(unit, 1)
    return int(number * mult)


@dataclass
class FeedItem:
    """Represents a single parsed feed item with enriched torrent metadata."""

    title: str
    link: str
    guid: str
    item_key: str
    published: Optional[str] = None
    category: Optional[str] = None
    size_str: Optional[str] = None
    size_bytes: Optional[int] = None
    seeds: Optional[int] = None
    leechers: Optional[int] = None
    torrent_url: Optional[str] = None
    info_hash: Optional[str] = None
    magnet_uri: Optional[str] = None
    description: Optional[str] = None
    canonical_media_key: Optional[str] = None

    @property
    def is_torrent(self) -> bool:
        """Return True if this item has torrent characteristics."""
        return bool(
            self.info_hash
            or self.torrent_url
            or self.magnet_uri
            or self.seeds is not None
        )


class FeedParser:
    """Fetches and parses RSS/Atom feeds, extracting BitTorrent and general metadata."""

    @staticmethod
    def build_magnet_uri(
        info_hash: str, title: str, trackers: Optional[List[str]] = None
    ) -> str:
        """Generate a standard BitTorrent magnet URI with trackers."""
        if not info_hash:
            return ""
        params = [("xt", f"urn:btih:{info_hash.upper()}"), ("dn", title)]
        trackers_to_use = trackers if trackers is not None else DEFAULT_TRACKERS
        for tr in trackers_to_use:
            params.append(("tr", tr))
        return "magnet:?" + urllib.parse.urlencode(params, safe=":")

    @staticmethod
    def normalize_media_title(title: str) -> str:
        """
        Normalize media release titles to a canonical identity for cross-group deduplication.
        Example: 'Deadpool.and.Wolverine.2024.1080p.WEB-DL.x265-Immortals'
        Returns: 'deadpool and wolverine 2024 1080p'
        """
        cleaned = title.strip()
        # Remove leading release group tags like [SubsPlease], [YTS.MX], [Erai-raws]
        cleaned = re.sub(r"^\s*\[[^\]]+\]\s*", "", cleaned)
        # Remove trailing hashes like [ABCD1234] or (ABCD1234)
        cleaned = re.sub(r"[\[\(][0-9a-fA-F]{8}[\]\)]", "", cleaned)

        # 1. Detect Resolution
        res_match = re.search(
            r"\b(2160p|4k|1080p|1080i|720p|576p|480p)\b", cleaned, re.IGNORECASE
        )
        res = res_match.group(1).lower() if res_match else ""
        if res == "4k":
            res = "2160p"

        # 2. Detect Season & Episode (e.g. S02E01, 2x01, Season 2 Episode 1)
        se_match = re.search(
            r"\b(?:s(\d{1,2})[.\s_-]*e(\d{1,3})|(\d{1,2})x(\d{1,3}))\b",
            cleaned,
            re.IGNORECASE,
        )
        se_tag = ""
        anime_match = None
        if se_match:
            s = int(se_match.group(1) or se_match.group(3))
            e = int(se_match.group(2) or se_match.group(4))
            se_tag = f"s{s:02d}e{e:02d}"
        else:
            # Detect Anime episode number: e.g. ' - 1100 ' or ' E1100 '
            anime_match = re.search(
                r"(?:-\s*(\d{2,4})|\be(\d{1,4})\b)", cleaned, re.IGNORECASE
            )
            if anime_match:
                ep = int(anime_match.group(1) or anime_match.group(2))
                se_tag = f"e{ep}"

        # 3. Detect Year (19xx or 20xx)
        year_match = re.search(r"\b(19\d\d|20\d\d)\b", cleaned)
        year = year_match.group(1) if year_match else ""

        # 4. Find cut-off position for base title
        cut_positions = []
        if se_match:
            cut_positions.append(se_match.start())
        elif anime_match:
            cut_positions.append(anime_match.start())
        if year_match:
            cut_positions.append(year_match.start())
        if res_match:
            cut_positions.append(res_match.start())

        if cut_positions:
            base_title = cleaned[: min(cut_positions)]
        else:
            base_title = cleaned

        # Clean punctuation and extra spaces
        base_title = re.sub(r"[\.\_\-\(\)\[\]\{\}\:]+", " ", base_title)
        base_title = re.sub(r"\s+", " ", base_title).strip().lower()

        parts = [base_title]
        if year:
            parts.append(year)
        if se_tag:
            parts.append(se_tag)
        if res:
            parts.append(res)
        return " ".join(parts)

    @classmethod
    def extract_item(cls, entry: Any) -> FeedItem:
        """Parse a single feedparser entry and extract all torrent/feed fields."""
        title = entry.get("title", "").strip() or "Untitled Item"
        link = entry.get("link", "").strip()
        guid = (
            entry.get("id", "").strip()
            or entry.get("guid", "").strip()
            or link
            or title
        )
        pub_date = (
            entry.get("published", "").strip() or entry.get("updated", "").strip()
        )

        # Category
        category = None
        tags = entry.get("tags", [])
        if tags and isinstance(tags, list) and len(tags) > 0:
            category = tags[0].get("term", "") or tags[0].get("label", "")
        if not category and entry.get("category"):
            category = str(entry.get("category"))

        description = entry.get("description", "") or entry.get("summary", "")

        # Extract size
        size_bytes = None
        size_str = None

        if "size" in entry:
            try:
                size_bytes = int(entry.size)
                size_str = format_bytes(size_bytes)
            except (ValueError, TypeError):
                pass

        # Check description for size (e.g. Size: 283.81 MB)
        if not size_str and description:
            size_match = re.search(
                r"Size:\s*([0-9.]+\s*[KMGT]?B)", description, re.IGNORECASE
            )
            if size_match:
                size_str = size_match.group(1).strip()

        # Extract seeds and leechers
        seeds = None
        leechers = None

        # 1. Custom XML tags (e.g. torrent:seeds, seeds)
        for key in ("torrent_seeds", "seeds"):
            if key in entry:
                try:
                    seeds = int(entry[key])
                    break
                except (ValueError, TypeError):
                    pass

        for key in ("torrent_peers", "torrent_leechers", "leechers", "peers"):
            if key in entry:
                try:
                    leechers = int(entry[key])
                    break
                except (ValueError, TypeError):
                    pass

        # 2. Extract from description or summary tags
        if description:
            if seeds is None:
                s_match = re.search(r"Seeds?:\s*(\d+)", description, re.IGNORECASE)
                if s_match:
                    seeds = int(s_match.group(1))
            if leechers is None:
                l_match = re.search(
                    r"(?:Leechers?|Peers?):\s*(\d+)", description, re.IGNORECASE
                )
                if l_match:
                    leechers = int(l_match.group(1))
            if not category:
                cat_match = re.search(
                    r"Category:\s*<[^>]+>([^<]+)</a>", description, re.IGNORECASE
                )
                if cat_match:
                    category = cat_match.group(1).strip()

        # Extract Torrent URL and InfoHash
        torrent_url = None
        info_hash = None
        magnet_uri = None

        # Check enclosures
        enclosures = entry.get("enclosures", [])
        for enc in enclosures:
            enc_href = enc.get("href", "") or enc.get("url", "")
            enc_type = enc.get("type", "").lower()
            enc_length = enc.get("length")

            if enc_length and not size_bytes:
                try:
                    size_bytes = int(enc_length)
                    if not size_str:
                        size_str = format_bytes(size_bytes)
                except (ValueError, TypeError):
                    pass

            if (
                "bittorrent" in enc_type
                or ".torrent" in enc_href
                or "torrent" in enc_type
            ):
                torrent_url = enc_href
                break

        # Check links
        if not torrent_url and "links" in entry:
            for l_obj in entry.links:
                href = l_obj.get("href", "")
                if ".torrent" in href or "bittorrent" in l_obj.get("type", ""):
                    torrent_url = href
                    break
                if href.startswith("magnet:"):
                    magnet_uri = href

        # Check if link itself is a magnet or torrent
        if link.startswith("magnet:"):
            magnet_uri = link
        elif link.endswith(".torrent"):
            torrent_url = link

        # Extract infohash from torrent_url or magnet
        if torrent_url:
            hash_match = re.search(r"([0-9a-fA-F]{40})", torrent_url)
            if hash_match:
                info_hash = hash_match.group(1).upper()

        if not info_hash and magnet_uri:
            hash_match = re.search(
                r"urn:btih:([0-9a-fA-F]{40}|[2-7a-zA-Z]{32})", magnet_uri, re.IGNORECASE
            )
            if hash_match:
                info_hash = hash_match.group(1).upper()

        if not info_hash and "torrent_infohash" in entry:
            info_hash = str(entry["torrent_infohash"]).strip().upper()

        # Build magnet URI if we have info_hash but no magnet yet
        if info_hash and not magnet_uri:
            magnet_uri = cls.build_magnet_uri(info_hash, title)

        # Generate a stable unique item_key for deduplication
        if info_hash:
            item_key = f"ih:{info_hash}"
        elif guid:
            item_key = f"guid:{guid}"
        elif link:
            item_key = f"link:{link}"
        else:
            item_key = f"hash:{hashlib.sha256((title + (pub_date or '')).encode('utf-8')).hexdigest()}"

        if not size_str and size_bytes:
            size_str = format_bytes(size_bytes)
        elif size_str and not size_bytes:
            size_bytes = parse_size_bytes(size_str)

        canonical_key = cls.normalize_media_title(title)

        return FeedItem(
            title=html.unescape(title),
            link=link,
            guid=guid,
            item_key=item_key,
            published=pub_date,
            category=html.unescape(category) if category else None,
            size_str=size_str,
            size_bytes=size_bytes,
            seeds=seeds,
            leechers=leechers,
            torrent_url=torrent_url,
            info_hash=info_hash,
            magnet_uri=magnet_uri,
            description=description,
            canonical_media_key=canonical_key,
        )

    @classmethod
    def _json_to_rss(cls, data: Any, feed_url: str = "") -> Optional[str]:
        """
        Convert JSON feed payloads (TorAPI, JSON Feed v1/v1.1, Jackett/Prowlarr JSON endpoints)
        into standard RSS 2.0 XML with BitTorrent namespaces.
        """
        items = []
        feed_title = "JSON Feed"

        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            feed_title = (
                data.get("title")
                or data.get("feed_title")
                or data.get("name")
                or "JSON Feed"
            )
            for k in ("items", "data", "results", "torrents", "list"):
                val = data.get(k)
                if isinstance(val, list):
                    items = val
                    break

        if not items:
            return None

        def _escape(val: Any) -> str:
            if val is None:
                return ""
            return html.escape(str(val), quote=True)

        entries = []
        for item in items:
            if not isinstance(item, dict):
                continue

            title = (
                item.get("Name")
                or item.get("name")
                or item.get("title")
                or item.get("Title")
                or "Untitled Item"
            )
            link = (
                item.get("Url")
                or item.get("url")
                or item.get("link")
                or item.get("Link")
                or item.get("details_url")
                or ""
            )
            torrent = (
                item.get("Torrent")
                or item.get("torrent")
                or item.get("DownloadUrl")
                or item.get("download_url")
                or item.get("MagnetUrl")
                or item.get("magnet")
                or item.get("enclosure")
                or ""
            )
            guid = (
                item.get("Id")
                or item.get("id")
                or item.get("Guid")
                or item.get("guid")
                or link
                or title
            )
            pubdate = (
                item.get("PubDate")
                or item.get("pubdate")
                or item.get("date_published")
                or item.get("created_at")
                or item.get("published")
                or ""
            )
            category = (
                item.get("Category")
                or item.get("category")
                or item.get("category_name")
                or item.get("CategoryName")
                or ""
            )
            info_hash = (
                item.get("Hash")
                or item.get("hash")
                or item.get("info_hash")
                or item.get("InfoHash")
                or ""
            )
            size = (
                item.get("Size")
                or item.get("size")
                or item.get("size_bytes")
                or item.get("length")
                or item.get("contentLength")
                or ""
            )
            seeds = (
                item.get("Seeds")
                or item.get("seeds")
                or item.get("seeders")
                or item.get("Seeders")
            )
            leechers = (
                item.get("Leechers")
                or item.get("leechers")
                or item.get("peers")
                or item.get("Peers")
            )

            # JSON Feed v1.1 attachments check
            attachments = item.get("attachments")
            if isinstance(attachments, list) and attachments:
                first_att = attachments[0]
                if isinstance(first_att, dict):
                    if not torrent:
                        torrent = first_att.get("url", "")
                    if not size:
                        size = first_att.get("size_in_bytes", "")

            desc = (
                item.get("description")
                or item.get("content_text")
                or item.get("content_html")
                or (f"Size: {size}" if size else "")
            )

            entry_xml = [
                "<item>",
                f"<title>{_escape(title)}</title>",
                f"<link>{_escape(link)}</link>",
                f'<guid isPermaLink="false">{_escape(guid)}</guid>',
            ]
            if pubdate:
                entry_xml.append(f"<pubDate>{_escape(pubdate)}</pubDate>")
            if category:
                entry_xml.append(f"<category>{_escape(category)}</category>")
            if torrent:
                if str(torrent).startswith("magnet:"):
                    if not link:
                        entry_xml.append(f"<link>{_escape(torrent)}</link>")
                else:
                    sz_attr = f' length="{_escape(size)}"' if size else ""
                    entry_xml.append(
                        f'<enclosure url="{_escape(torrent)}" type="application/x-bittorrent"{sz_attr}/>'
                    )
            if info_hash:
                entry_xml.append(
                    f"<torrent:infoHash>{_escape(info_hash)}</torrent:infoHash>"
                )
            if seeds is not None:
                entry_xml.append(f"<torrent:seeds>{_escape(seeds)}</torrent:seeds>")
            if leechers is not None:
                entry_xml.append(f"<torrent:peers>{_escape(leechers)}</torrent:peers>")
            if size:
                entry_xml.append(
                    f"<torrent:contentLength>{_escape(size)}</torrent:contentLength>"
                )
            if desc:
                entry_xml.append(f"<description>{_escape(desc)}</description>")
            entry_xml.append("</item>")

            entries.append("\n".join(entry_xml))

        entries_str = "\n".join(entries)
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<rss version="2.0" xmlns:torrent="http://xmlns.ezrss.it/0.1/dtd/">\n'
            "<channel>\n"
            f"<title>{_escape(feed_title)}</title>\n"
            f"<link>{_escape(feed_url)}</link>\n"
            f"{entries_str}\n"
            "</channel>\n"
            "</rss>"
        )

    @classmethod
    async def fetch_and_parse(
        cls,
        session: aiohttp.ClientSession,
        url: str,
        etag: Optional[str] = None,
        modified: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[str], Optional[str], List[FeedItem], int]:
        """
        Fetch RSS/Atom XML or JSON feeds asynchronously with HTTP conditional GET headers.
        Returns (feed_title, new_etag, new_modified, list_of_items, status_code).
        """
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/xml, text/xml, application/atom+xml, application/json, text/html, */*",
        }
        if etag:
            headers["If-None-Match"] = etag
        if modified:
            headers["If-Modified-Since"] = modified

        timeout = aiohttp.ClientTimeout(total=20)
        try:
            async with session.get(url, headers=headers, timeout=timeout) as resp:
                status = resp.status
                if status == 304:
                    # Content has not changed
                    return None, etag, modified, [], 304

                if status != 200:
                    logger.warning("Feed %s returned HTTP status %d", url, status)
                    return None, None, None, [], status

                new_etag = resp.headers.get("ETag")
                new_modified = resp.headers.get("Last-Modified")
                content = await resp.read()

                # Check if payload is JSON (TorAPI, JSON Feed v1, Jackett/Prowlarr endpoints)
                rss_xml = None
                stripped = content.strip()
                if stripped.startswith(b"{") or stripped.startswith(b"["):
                    try:
                        json_data = json.loads(
                            content.decode("utf-8", errors="replace")
                        )
                        rss_xml = cls._json_to_rss(json_data, feed_url=url)
                    except Exception as e:
                        logger.debug(
                            "Content from %s is not JSON (%s), parsing as XML", url, e
                        )

                # Parse XML using feedparser
                parsed = feedparser.parse(
                    rss_xml.encode("utf-8") if rss_xml else content
                )
                feed_title = parsed.feed.get("title", "").strip() or url

                items = [cls.extract_item(entry) for entry in parsed.entries]
                return feed_title, new_etag, new_modified, items, 200

        except Exception as e:
            logger.error("Error fetching feed from %s: %s", url, e)
            return None, None, None, [], 0

    @staticmethod
    def _check_match(text: str, pattern: str, case_sensitive: bool = False) -> bool:
        """Check if pattern matches text via regex or substring."""
        pattern = pattern.strip()
        if not pattern:
            return False
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            if re.search(pattern, text, flags):
                return True
        except re.error:
            pass
        if case_sensitive:
            return pattern in text
        return pattern.lower() in text.lower()

    @staticmethod
    def _parse_filter_groups(filter_str: str) -> List[List[str]]:
        """
        Parse a filter string into conjunction groups (AND) of alternatives (OR).
        Syntax:
          "group1 | group2 | group3" -> all groups must match (AND)
          "term1 or term2, term3"    -> at least one alternative in the group must match (OR)
        Example:
          "1080p or 720p | PSA | hevc or x265"
          -> [["1080p", "720p"], ["PSA"], ["hevc", "x265"]]
        """
        if not filter_str or not filter_str.strip():
            return []

        # Split on pipe '|' to get AND groups
        raw_groups = filter_str.split("|")
        conjunction_groups = []

        for grp in raw_groups:
            grp = grp.strip()
            if not grp:
                continue

            # Split within group on ' or ', ' OR ', or ',' for alternatives
            alternatives = re.split(r",|\s+(?:or|OR)\s+", grp)
            cleaned_alts = [a.strip() for a in alternatives if a.strip()]
            if cleaned_alts:
                conjunction_groups.append(cleaned_alts)

        return conjunction_groups

    @staticmethod
    def matches_filters(
        title: str,
        filter_keywords: Optional[str] = None,
        filter_exclude: Optional[str] = None,
        case_sensitive: bool = False,
    ) -> bool:
        """
        Check if an item title passes inclusion and exclusion keyword filters.
        Supports advanced boolean grouping:
          - '|' acts as AND (every pipe-separated group must be satisfied)
          - 'or' and ',' act as OR (any alternative within a group satisfies that group)
          - For exclude filters: any matching term or compound term ('word1 + word2') excludes the item.
        """
        if not title:
            return False

        # 1. Check required inclusion groups: EVERY group must have at least one match
        if filter_keywords and filter_keywords.strip():
            groups = FeedParser._parse_filter_groups(filter_keywords)
            for grp in groups:
                if not any(
                    FeedParser._check_match(title, kw, case_sensitive) for kw in grp
                ):
                    return False

        # 2. Check exclusion filters: NO exclusion rule should match
        if filter_exclude and filter_exclude.strip():
            raw_excludes = re.split(r"[,|]|\s+(?:or|OR)\s+", filter_exclude)
            for ex in raw_excludes:
                ex = ex.strip()
                if not ex:
                    continue
                # Support compound exclusion e.g. "hindi + dubbed" or "hindi & dubbed"
                if " + " in ex or " & " in ex:
                    sub_parts = re.split(r"\s+[+&]\s+", ex)
                    if all(
                        FeedParser._check_match(title, sp, case_sensitive)
                        for sp in sub_parts
                        if sp.strip()
                    ):
                        return False
                else:
                    if FeedParser._check_match(title, ex, case_sensitive):
                        return False

        return True
