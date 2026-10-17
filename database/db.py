import aiosqlite
import logging
from typing import Optional, List, Dict, Any, Tuple

logger = logging.getLogger(__name__)


class Database:
    """Async SQLite database manager for feeds, subscriptions, and seen items."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    async def init_db(self) -> None:
        """Create tables and indexes if they do not exist."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON;")

            # Feeds table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS feeds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT UNIQUE NOT NULL,
                    title TEXT,
                    last_checked TIMESTAMP,
                    etag TEXT,
                    modified TEXT,
                    is_active INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # Subscriptions table: links chat/channel on Telegram or Discord to a feed
            await db.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    feed_id INTEGER NOT NULL,
                    platform TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    filter_keywords TEXT,
                    filter_exclude TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (feed_id) REFERENCES feeds(id) ON DELETE CASCADE,
                    UNIQUE(feed_id, platform, target_id)
                );
            """)

            # Seen items table: prevents duplicate alerts
            await db.execute("""
                CREATE TABLE IF NOT EXISTS seen_items (
                    feed_id INTEGER NOT NULL,
                    item_key TEXT NOT NULL,
                    title TEXT,
                    seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (feed_id, item_key),
                    FOREIGN KEY (feed_id) REFERENCES feeds(id) ON DELETE CASCADE
                );
            """)

            # Helpful indexes
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_subs_target ON subscriptions(platform, target_id);"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_subs_feed ON subscriptions(feed_id);"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_seen_feed ON seen_items(feed_id);"
            )

            await db.commit()
            logger.info("Database initialized successfully at %s", self.db_path)

    async def add_or_get_feed(
        self, url: str, title: Optional[str] = None
    ) -> Tuple[int, str]:
        """Add a feed if not exists, or return existing feed (id, title)."""
        url = url.strip()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT id, title FROM feeds WHERE url = ?", (url,)
            )
            row = await cursor.fetchone()
            if row:
                feed_id = row["id"]
                current_title = row["title"] or title or url
                if title and row["title"] != title:
                    await db.execute(
                        "UPDATE feeds SET title = ? WHERE id = ?", (title, feed_id)
                    )
                    await db.commit()
                return feed_id, current_title

            cursor = await db.execute(
                "INSERT INTO feeds (url, title, is_active) VALUES (?, ?, 1)",
                (url, title or url),
            )
            await db.commit()
            feed_id = cursor.lastrowid
            return feed_id, title or url

    async def subscribe(
        self,
        platform: str,
        target_id: str,
        url: str,
        title: Optional[str] = None,
        filter_keywords: Optional[str] = None,
        filter_exclude: Optional[str] = None,
    ) -> Tuple[int, str, bool]:
        """
        Subscribe a target (chat or channel) to a feed.
        Returns (feed_id, feed_title, is_new_subscription).
        """
        feed_id, feed_title = await self.add_or_get_feed(url, title)
        target_id = str(target_id)
        platform = platform.lower()

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT id FROM subscriptions WHERE feed_id = ? AND platform = ? AND target_id = ?",
                (feed_id, platform, target_id),
            )
            existing = await cursor.fetchone()
            if existing:
                # Update filters if provided
                if filter_keywords is not None or filter_exclude is not None:
                    await db.execute(
                        """
                        UPDATE subscriptions
                        SET filter_keywords = COALESCE(?, filter_keywords),
                            filter_exclude = COALESCE(?, filter_exclude)
                        WHERE id = ?
                        """,
                        (filter_keywords, filter_exclude, existing[0]),
                    )
                    await db.commit()
                return feed_id, feed_title, False

            await db.execute(
                """
                INSERT INTO subscriptions (feed_id, platform, target_id, filter_keywords, filter_exclude)
                VALUES (?, ?, ?, ?, ?)
                """,
                (feed_id, platform, target_id, filter_keywords, filter_exclude),
            )
            # Ensure feed is active
            await db.execute("UPDATE feeds SET is_active = 1 WHERE id = ?", (feed_id,))
            await db.commit()
            return feed_id, feed_title, True

    async def unsubscribe(
        self, platform: str, target_id: str, feed_identifier: str
    ) -> Optional[str]:
        """
        Unsubscribe a target by feed ID or feed URL.
        Returns the removed feed title/url if found, or None.
        """
        target_id = str(target_id)
        platform = platform.lower()
        feed_identifier = feed_identifier.strip()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            # Find matching feed
            if feed_identifier.isdigit():
                cursor = await db.execute(
                    """
                    SELECT s.id as sub_id, f.id as feed_id, f.title, f.url
                    FROM subscriptions s
                    JOIN feeds f ON s.feed_id = f.id
                    WHERE s.platform = ? AND s.target_id = ? AND (f.id = ? OR s.id = ?)
                    """,
                    (platform, target_id, int(feed_identifier), int(feed_identifier)),
                )
            else:
                cursor = await db.execute(
                    """
                    SELECT s.id as sub_id, f.id as feed_id, f.title, f.url
                    FROM subscriptions s
                    JOIN feeds f ON s.feed_id = f.id
                    WHERE s.platform = ? AND s.target_id = ? AND (f.url = ? OR f.title LIKE ?)
                    """,
                    (platform, target_id, feed_identifier, f"%{feed_identifier}%"),
                )

            row = await cursor.fetchone()
            if not row:
                return None

            sub_id = row["sub_id"]
            feed_id = row["feed_id"]
            feed_label = row["title"] or row["url"]

            await db.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))
            await db.commit()

            # Check if any subscribers remain for this feed
            count_cursor = await db.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE feed_id = ?", (feed_id,)
            )
            count = (await count_cursor.fetchone())[0]
            if count == 0:
                # Mark inactive so we don't keep polling it
                await db.execute(
                    "UPDATE feeds SET is_active = 0 WHERE id = ?", (feed_id,)
                )
                await db.commit()

            return feed_label

    async def update_filters(
        self,
        platform: str,
        target_id: str,
        feed_id: int,
        filter_keywords: Optional[str] = None,
        filter_exclude: Optional[str] = None,
    ) -> bool:
        """Update keyword filter rules for a specific subscription."""
        target_id = str(target_id)
        platform = platform.lower()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                UPDATE subscriptions
                SET filter_keywords = CASE WHEN ? IS NOT NULL THEN ? ELSE filter_keywords END,
                    filter_exclude = CASE WHEN ? IS NOT NULL THEN ? ELSE filter_exclude END
                WHERE platform = ? AND target_id = ? AND feed_id = ?
                """,
                (
                    filter_keywords,
                    filter_keywords,
                    filter_exclude,
                    filter_exclude,
                    platform,
                    target_id,
                    feed_id,
                ),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def list_subscriptions(
        self, platform: str, target_id: str
    ) -> List[Dict[str, Any]]:
        """List all feeds subscribed by a specific chat/channel."""
        target_id = str(target_id)
        platform = platform.lower()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT f.id as feed_id, f.url, f.title, f.last_checked,
                       s.id as sub_id, s.filter_keywords, s.filter_exclude, s.created_at
                FROM subscriptions s
                JOIN feeds f ON s.feed_id = f.id
                WHERE s.platform = ? AND s.target_id = ?
                ORDER BY s.id ASC
                """,
                (platform, target_id),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_active_feeds(self) -> List[Dict[str, Any]]:
        """Return all active feeds that have at least one subscriber."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT DISTINCT f.id, f.url, f.title, f.last_checked, f.etag, f.modified
                FROM feeds f
                JOIN subscriptions s ON f.id = s.feed_id
                WHERE f.is_active = 1
                ORDER BY f.id ASC
                """
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_subscribers_for_feed(self, feed_id: int) -> List[Dict[str, Any]]:
        """Get all subscriber targets (Telegram / Discord) for a given feed."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT platform, target_id, filter_keywords, filter_exclude
                FROM subscriptions
                WHERE feed_id = ?
                """,
                (feed_id,),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def has_seen_any_items(self, feed_id: int) -> bool:
        """Check if we have already recorded any seen items for this feed."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT 1 FROM seen_items WHERE feed_id = ? LIMIT 1", (feed_id,)
            )
            row = await cursor.fetchone()
            return row is not None

    async def is_item_seen(self, feed_id: int, item_key: str) -> bool:
        """Check if a specific item has already been marked as seen."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT 1 FROM seen_items WHERE feed_id = ? AND item_key = ?",
                (feed_id, item_key),
            )
            row = await cursor.fetchone()
            return row is not None

    async def mark_items_seen(
        self, feed_id: int, items: List[Tuple[str, Optional[str]]]
    ) -> None:
        """Batch mark items as seen (item_key, title)."""
        if not items:
            return
        async with aiosqlite.connect(self.db_path) as db:
            await db.executemany(
                """
                INSERT OR IGNORE INTO seen_items (feed_id, item_key, title)
                VALUES (?, ?, ?)
                """,
                [(feed_id, key, title) for key, title in items],
            )
            await db.commit()

    async def update_feed_checked(
        self,
        feed_id: int,
        title: Optional[str] = None,
        etag: Optional[str] = None,
        modified: Optional[str] = None,
    ) -> None:
        """Update last_checked timestamp, etag, and modified headers."""
        async with aiosqlite.connect(self.db_path) as db:
            if title:
                await db.execute(
                    """
                    UPDATE feeds
                    SET last_checked = CURRENT_TIMESTAMP,
                        title = ?,
                        etag = COALESCE(?, etag),
                        modified = COALESCE(?, modified)
                    WHERE id = ?
                    """,
                    (title, etag, modified, feed_id),
                )
            else:
                await db.execute(
                    """
                    UPDATE feeds
                    SET last_checked = CURRENT_TIMESTAMP,
                        etag = COALESCE(?, etag),
                        modified = COALESCE(?, modified)
                    WHERE id = ?
                    """,
                    (etag, modified, feed_id),
                )
            await db.commit()

    async def cleanup_old_seen_items(self, days: int = 14) -> int:
        """Purge seen items older than N days to prevent database bloating."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM seen_items WHERE seen_at < datetime('now', ?)",
                (f"-{days} days",),
            )
            await db.commit()
            return cursor.rowcount
