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
    async def fetch_and_parse(
        cls,
        session: aiohttp.ClientSession,
        url: str,
        etag: Optional[str] = None,
        modified: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[str], Optional[str], List[FeedItem], int]:
        """
        Fetch RSS/Atom XML asynchronously with HTTP conditional GET headers.
        Returns (feed_title, new_etag, new_modified, list_of_items, status_code).
        """
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/xml, text/xml, application/atom+xml, text/html, */*",
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

                # Parse XML using feedparser
                parsed = feedparser.parse(content)
                feed_title = parsed.feed.get("title", "").strip() or url

                items = [cls.extract_item(entry) for entry in parsed.entries]
                return feed_title, new_etag, new_modified, items, 200

        except Exception as e:
            logger.error("Error fetching feed from %s: %s", url, e)
            return None, None, None, [], 0

    @staticmethod
    def _check_match(text: str, pattern: str) -> bool:
        """Check if pattern matches text via regex or case-insensitive substring."""
        pattern = pattern.strip()
        if not pattern:
            return False
        try:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        except re.error:
            pass
        return pattern.lower() in text.lower()

    @staticmethod
    def matches_filters(
        title: str,
        filter_keywords: Optional[str] = None,
        filter_exclude: Optional[str] = None,
    ) -> bool:
        """
        Check if an item title passes inclusion and exclusion keyword filters.
        filter_keywords: comma or pipe separated keywords, at least one MUST match (case-insensitive or regex)
        filter_exclude: comma or pipe separated keywords, NONE must match (case-insensitive or regex)
        """
        # Check required keywords (ANY of the comma/pipe-separated keywords or regex must match)
        if filter_keywords:
            raw_patterns = re.split(r"[,|]", filter_keywords)
            keywords = [p.strip() for p in raw_patterns if p.strip()]
            if keywords and not any(
                FeedParser._check_match(title, kw) for kw in keywords
            ):
                return False

        # Check excluded keywords (NONE of the keywords may match)
        if filter_exclude:
            raw_patterns = re.split(r"[,|]", filter_exclude)
            excludes = [p.strip() for p in raw_patterns if p.strip()]
            if any(FeedParser._check_match(title, ex) for ex in excludes):
                return False

        return True
