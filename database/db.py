import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Tuple
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ReturnDocument, ASCENDING
from pymongo.errors import DuplicateKeyError, BulkWriteError

logger = logging.getLogger(__name__)


class Database:
    """Asynchronous MongoDB database manager for feeds, subscriptions, and seen items."""

    def __init__(self, db_url: str, db_name: str = "FeedPulse"):
        self.db_url = db_url.strip()
        self.db_name = db_name
        self.client: Optional[AsyncIOMotorClient] = None
        self.db: Optional[AsyncIOMotorDatabase] = None

    async def init_db(self) -> None:
        """Connect to MongoDB and initialize collections, indexes, and counters."""
        if not self.db_url:
            raise ValueError(
                "DATABASE_URL is empty! Please provide a valid MongoDB connection string."
            )

        self.client = AsyncIOMotorClient(self.db_url, serverSelectionTimeoutMS=5000)
        self.db = self.client[self.db_name]

        # Verify connectivity
        await self.client.admin.command("ping")
        logger.info("Successfully connected to MongoDB (%s)", self.db_name)

        # 1. Feeds collection indexes
        await self.db.feeds.create_index([("url", ASCENDING)], unique=True)
        await self.db.feeds.create_index([("feed_id", ASCENDING)], unique=True)
        await self.db.feeds.create_index([("is_active", ASCENDING)])

        # 2. Subscriptions collection indexes
        await self.db.subscriptions.create_index(
            [("feed_id", ASCENDING), ("platform", ASCENDING), ("target_id", ASCENDING)],
            unique=True,
        )
        await self.db.subscriptions.create_index(
            [("platform", ASCENDING), ("target_id", ASCENDING)]
        )

        # 3. Seen items collection indexes with TTL auto-expiration (14 days)
        await self.db.seen_items.create_index(
            [("feed_id", ASCENDING), ("item_key", ASCENDING)], unique=True
        )
        await self.db.seen_items.create_index(
            [("seen_at", ASCENDING)],
            expireAfterSeconds=14
            * 86400,  # Automatically removes items older than 14 days
        )

        logger.info("MongoDB collections and indexes initialized successfully")

    async def _get_next_sequence(self, name: str = "feed_id") -> int:
        """Generate auto-incrementing integer sequence for clean numeric IDs (e.g. /remove 1)."""
        ret = await self.db.counters.find_one_and_update(
            {"_id": name},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return ret["seq"]

    async def add_or_get_feed(
        self, url: str, title: Optional[str] = None
    ) -> Tuple[int, str]:
        """Add a feed if not exists, or return existing feed (feed_id, title)."""
        url = url.strip()
        existing = await self.db.feeds.find_one({"url": url})
        if existing:
            feed_id = existing["feed_id"]
            current_title = existing.get("title") or title or url
            if title and existing.get("title") != title:
                await self.db.feeds.update_one(
                    {"_id": existing["_id"]}, {"$set": {"title": title}}
                )
            return feed_id, current_title

        feed_id = await self._get_next_sequence("feed_id")
        doc = {
            "feed_id": feed_id,
            "url": url,
            "title": title or url,
            "last_checked": None,
            "etag": None,
            "modified": None,
            "is_active": True,
            "created_at": datetime.now(timezone.utc),
        }
        await self.db.feeds.insert_one(doc)
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

        existing = await self.db.subscriptions.find_one(
            {"feed_id": feed_id, "platform": platform, "target_id": target_id}
        )

        if existing:
            # Update filters if provided
            update_fields = {}
            if filter_keywords is not None:
                update_fields["filter_keywords"] = filter_keywords
            if filter_exclude is not None:
                update_fields["filter_exclude"] = filter_exclude
            if update_fields:
                await self.db.subscriptions.update_one(
                    {"_id": existing["_id"]}, {"$set": update_fields}
                )
            return feed_id, feed_title, False

        doc = {
            "feed_id": feed_id,
            "platform": platform,
            "target_id": target_id,
            "filter_keywords": filter_keywords,
            "filter_exclude": filter_exclude,
            "created_at": datetime.now(timezone.utc),
        }
        try:
            await self.db.subscriptions.insert_one(doc)
        except DuplicateKeyError:
            return feed_id, feed_title, False

        # Ensure feed is marked active
        await self.db.feeds.update_one(
            {"feed_id": feed_id}, {"$set": {"is_active": True}}
        )
        return feed_id, feed_title, True

    async def unsubscribe(
        self, platform: str, target_id: str, feed_identifier: str
    ) -> Optional[str]:
        """
        Unsubscribe a target by feed ID or feed URL/title.
        Returns the removed feed title/url if found, or None.
        """
        target_id = str(target_id)
        platform = platform.lower()
        feed_identifier = feed_identifier.strip()

        # Find the feed document
        if feed_identifier.isdigit():
            feed_doc = await self.db.feeds.find_one({"feed_id": int(feed_identifier)})
        else:
            feed_doc = await self.db.feeds.find_one(
                {
                    "$or": [
                        {"url": feed_identifier},
                        {"title": {"$regex": feed_identifier, "$options": "i"}},
                    ]
                }
            )

        if not feed_doc:
            return None

        feed_id = feed_doc["feed_id"]
        feed_label = feed_doc.get("title") or feed_doc.get("url")

        res = await self.db.subscriptions.delete_one(
            {"feed_id": feed_id, "platform": platform, "target_id": target_id}
        )

        if res.deleted_count == 0:
            return None

        # If no subscribers left for this feed, mark inactive
        active_sub_count = await self.db.subscriptions.count_documents(
            {"feed_id": feed_id}
        )
        if active_sub_count == 0:
            await self.db.feeds.update_one(
                {"feed_id": feed_id}, {"$set": {"is_active": False}}
            )

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

        update_fields = {}
        if filter_keywords is not None:
            update_fields["filter_keywords"] = filter_keywords
        if filter_exclude is not None:
            update_fields["filter_exclude"] = filter_exclude

        if not update_fields:
            return False

        res = await self.db.subscriptions.update_one(
            {"platform": platform, "target_id": target_id, "feed_id": feed_id},
            {"$set": update_fields},
        )
        return res.modified_count > 0 or res.matched_count > 0

    async def list_subscriptions(
        self, platform: str, target_id: str
    ) -> List[Dict[str, Any]]:
        """List all feeds subscribed by a specific chat/channel."""
        target_id = str(target_id)
        platform = platform.lower()

        pipeline = [
            {"$match": {"platform": platform, "target_id": target_id}},
            {
                "$lookup": {
                    "from": "feeds",
                    "localField": "feed_id",
                    "foreignField": "feed_id",
                    "as": "feed_info",
                }
            },
            {"$unwind": "$feed_info"},
            {
                "$project": {
                    "feed_id": "$feed_id",
                    "url": "$feed_info.url",
                    "title": "$feed_info.title",
                    "last_checked": "$feed_info.last_checked",
                    "filter_keywords": "$filter_keywords",
                    "filter_exclude": "$filter_exclude",
                    "created_at": "$created_at",
                }
            },
            {"$sort": {"feed_id": ASCENDING}},
        ]

        cursor = self.db.subscriptions.aggregate(pipeline)
        return await cursor.to_list(length=500)

    async def get_active_feeds(self) -> List[Dict[str, Any]]:
        """Return all active feeds that have at least one subscriber."""
        cursor = self.db.feeds.find({"is_active": True}).sort("feed_id", ASCENDING)
        raw_feeds = await cursor.to_list(length=1000)
        # Adapt document fields to standard interface (e.g. id = feed_id)
        feeds = []
        for f in raw_feeds:
            f["id"] = f["feed_id"]
            feeds.append(f)
        return feeds

    async def get_subscribers_for_feed(self, feed_id: int) -> List[Dict[str, Any]]:
        """Get all subscriber targets (Telegram / Discord) for a given feed."""
        cursor = self.db.subscriptions.find({"feed_id": feed_id})
        return await cursor.to_list(length=1000)

    async def has_seen_any_items(self, feed_id: int) -> bool:
        """Check if we have already recorded any seen items for this feed."""
        doc = await self.db.seen_items.find_one({"feed_id": feed_id})
        return doc is not None

    async def is_item_seen(self, feed_id: int, item_key: str) -> bool:
        """Check if a specific item has already been marked as seen."""
        doc = await self.db.seen_items.find_one(
            {"feed_id": feed_id, "item_key": item_key}
        )
        return doc is not None

    async def mark_items_seen(
        self, feed_id: int, items: List[Tuple[str, Optional[str]]]
    ) -> None:
        """Batch mark items as seen (item_key, title)."""
        if not items:
            return

        now = datetime.now(timezone.utc)
        docs = [
            {"feed_id": feed_id, "item_key": key, "title": title, "seen_at": now}
            for key, title in items
        ]

        try:
            await self.db.seen_items.insert_many(docs, ordered=False)
        except BulkWriteError:
            # Ignore duplicate key write errors
            pass

    async def update_feed_checked(
        self,
        feed_id: int,
        title: Optional[str] = None,
        etag: Optional[str] = None,
        modified: Optional[str] = None,
    ) -> None:
        """Update last_checked timestamp, etag, and modified headers."""
        now = datetime.now(timezone.utc)
        update_fields: Dict[str, Any] = {"last_checked": now}

        if title:
            update_fields["title"] = title
        if etag:
            update_fields["etag"] = etag
        if modified:
            update_fields["modified"] = modified

        await self.db.feeds.update_one({"feed_id": feed_id}, {"$set": update_fields})

    async def cleanup_old_seen_items(self, days: int = 14) -> int:
        """Purge seen items older than N days (MongoDB TTL index handles this automatically as well)."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        res = await self.db.seen_items.delete_many({"seen_at": {"$lt": cutoff}})
        return res.deleted_count
