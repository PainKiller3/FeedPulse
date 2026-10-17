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

        # 2. Subscriptions collection indexes (includes thread_id and user_id for isolation)
        try:
            # Check existing indexes to drop legacy unique index if needed
            index_info = await self.db.subscriptions.index_information()
            for idx_name, idx_meta in index_info.items():
                if idx_name == "_id_":
                    continue
                keys = idx_meta.get("key", [])
                key_names = [k[0] for k in keys]
                if (
                    idx_meta.get("unique")
                    and "user_id" not in key_names
                    and "feed_id" in key_names
                ):
                    logger.info(
                        "Dropping legacy subscriptions unique index: %s", idx_name
                    )
                    await self.db.subscriptions.drop_index(idx_name)
        except Exception as e:
            logger.debug("Index migration check note: %s", e)

        try:
            await self.db.subscriptions.create_index(
                [
                    ("feed_id", ASCENDING),
                    ("platform", ASCENDING),
                    ("target_id", ASCENDING),
                    ("thread_id", ASCENDING),
                    ("user_id", ASCENDING),
                ],
                unique=True,
            )
        except Exception as e:
            logger.warning("Could not create subscriptions unique index: %s", e)

        await self.db.subscriptions.create_index(
            [("platform", ASCENDING), ("target_id", ASCENDING), ("user_id", ASCENDING)]
        )
        await self.db.subscriptions.create_index([("user_id", ASCENDING)])
        await self.db.subscriptions.create_index([("is_paused", ASCENDING)])

        # 3. Seen items collection indexes with TTL auto-expiration (14 days)
        await self.db.seen_items.create_index(
            [("feed_id", ASCENDING), ("item_key", ASCENDING)], unique=True
        )
        await self.db.seen_items.create_index(
            [("seen_at", ASCENDING)],
            expireAfterSeconds=14
            * 86400,  # Automatically removes items older than 14 days
        )

        # 4. Alerted media collection indexes for cross-group deduplication with TTL (14 days)
        await self.db.alerted_media.create_index(
            [
                ("platform", ASCENDING),
                ("target_id", ASCENDING),
                ("thread_id", ASCENDING),
                ("canonical_key", ASCENDING),
            ],
            unique=True,
        )
        await self.db.alerted_media.create_index(
            [("alerted_at", ASCENDING)],
            expireAfterSeconds=14 * 86400,
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
        thread_id: Optional[int] = None,
        user_id: Optional[int] = None,
        custom_title: Optional[str] = None,
        case_sensitive: bool = False,
    ) -> Tuple[int, str, bool]:
        """
        Subscribe a target (chat, channel, topic, and user) to a feed.
        Returns (feed_id, feed_title, is_new_subscription).
        """
        feed_id, feed_title = await self.add_or_get_feed(url, title)
        target_id = str(target_id)
        platform = platform.lower()

        sub_query = {
            "feed_id": feed_id,
            "platform": platform,
            "target_id": target_id,
            "thread_id": thread_id,
            "user_id": user_id,
        }
        existing = await self.db.subscriptions.find_one(sub_query)

        if existing:
            # Update filters if provided
            update_fields = {}
            if filter_keywords is not None:
                update_fields["filter_keywords"] = filter_keywords
            if filter_exclude is not None:
                update_fields["filter_exclude"] = filter_exclude
            if custom_title is not None:
                update_fields["custom_title"] = custom_title
            if case_sensitive is not None:
                update_fields["case_sensitive"] = case_sensitive
            if update_fields:
                await self.db.subscriptions.update_one(
                    {"_id": existing["_id"]}, {"$set": update_fields}
                )
            return feed_id, feed_title, False

        doc = {
            "feed_id": feed_id,
            "platform": platform,
            "target_id": target_id,
            "thread_id": thread_id,
            "user_id": user_id,
            "custom_title": custom_title,
            "case_sensitive": case_sensitive,
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
        self,
        platform: str,
        target_id: str,
        feed_identifier: str,
        thread_id: Optional[int] = None,
        user_id: Optional[int] = None,
        is_admin: bool = False,
    ) -> Tuple[Optional[str], str]:
        """
        Unsubscribe a target by feed ID or feed URL/title with ownership verification.
        Returns (feed_label, status_code) where status_code is 'success', 'permission_denied', or 'not_found'.
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
            return None, "not_found"

        feed_id = feed_doc["feed_id"]
        feed_label = feed_doc.get("title") or feed_doc.get("url")

        base_query = {
            "feed_id": feed_id,
            "platform": platform,
            "target_id": target_id,
        }
        if thread_id is not None:
            base_query["thread_id"] = thread_id

        is_private = (user_id is not None) and (str(target_id) == str(user_id))

        # Find all subscriptions matching this feed in this target
        existing_subs = await self.db.subscriptions.find(base_query).to_list(length=100)
        if not existing_subs:
            # If in private DM and no sub found directly in DM, check if user owns this feed in another chat
            if is_private and user_id is not None:
                user_subs = await self.db.subscriptions.find(
                    {"feed_id": feed_id, "platform": platform, "user_id": user_id}
                ).to_list(length=10)
                if user_subs:
                    existing_subs = user_subs
            if not existing_subs:
                return None, "not_found"

        # Check ownership unless caller has admin/owner privileges or is in private chat
        if not is_admin and not is_private and user_id is not None:
            user_sub = next(
                (s for s in existing_subs if s.get("user_id") == user_id), None
            )
            if not user_sub:
                # Feed exists in this chat, but belongs to another user
                return feed_label, "permission_denied"
            del_query = {"_id": user_sub["_id"]}
        else:
            # Admin or private chat: delete user's sub if matched, otherwise first sub in target
            if user_id is not None:
                user_sub = next(
                    (s for s in existing_subs if s.get("user_id") == user_id), None
                )
                del_query = (
                    {"_id": user_sub["_id"]}
                    if user_sub
                    else {"_id": existing_subs[0]["_id"]}
                )
            else:
                del_query = {"_id": existing_subs[0]["_id"]}

        res = await self.db.subscriptions.delete_one(del_query)
        if res.deleted_count == 0:
            return None, "not_found"

        # If no subscribers left across all targets for this feed, mark inactive
        active_sub_count = await self.db.subscriptions.count_documents(
            {"feed_id": feed_id}
        )
        if active_sub_count == 0:
            await self.db.feeds.update_one(
                {"feed_id": feed_id}, {"$set": {"is_active": False}}
            )

        return feed_label, "success"

    async def update_filters(
        self,
        platform: str,
        target_id: str,
        feed_id: int,
        filter_keywords: Optional[str] = None,
        filter_exclude: Optional[str] = None,
        thread_id: Optional[int] = None,
        user_id: Optional[int] = None,
        is_admin: bool = False,
    ) -> str:
        """
        Update keyword filter rules for a specific subscription.
        Returns 'success', 'permission_denied', or 'not_found'.
        """
        target_id = str(target_id)
        platform = platform.lower()

        update_fields = {}
        if filter_keywords is not None:
            update_fields["filter_keywords"] = filter_keywords
        if filter_exclude is not None:
            update_fields["filter_exclude"] = filter_exclude

        if not update_fields:
            return "not_found"

        base_query = {
            "platform": platform,
            "target_id": target_id,
            "feed_id": feed_id,
        }
        if thread_id is not None:
            base_query["thread_id"] = thread_id

        is_private = (user_id is not None) and (str(target_id) == str(user_id))
        existing_subs = await self.db.subscriptions.find(base_query).to_list(length=100)
        if not existing_subs:
            # If in private DM and no sub found directly in DM, check if user owns this feed in another chat
            if is_private and user_id is not None:
                user_subs = await self.db.subscriptions.find(
                    {"feed_id": feed_id, "platform": platform, "user_id": user_id}
                ).to_list(length=10)
                if user_subs:
                    existing_subs = user_subs
            if not existing_subs:
                return "not_found"
        if not is_admin and not is_private and user_id is not None:
            user_sub = next(
                (s for s in existing_subs if s.get("user_id") == user_id), None
            )
            if not user_sub:
                return "permission_denied"
            upd_query = {"_id": user_sub["_id"]}
        else:
            if user_id is not None:
                user_sub = next(
                    (s for s in existing_subs if s.get("user_id") == user_id), None
                )
                upd_query = (
                    {"_id": user_sub["_id"]}
                    if user_sub
                    else {"_id": existing_subs[0]["_id"]}
                )
            else:
                upd_query = {"_id": existing_subs[0]["_id"]}

        res = await self.db.subscriptions.update_one(
            upd_query,
            {"$set": update_fields},
        )
        return (
            "success"
            if (res.modified_count > 0 or res.matched_count > 0)
            else "not_found"
        )

    async def set_subscription_pause(
        self,
        platform: str,
        target_id: str,
        feed_identifier: str,
        paused: bool,
        thread_id: Optional[int] = None,
        user_id: Optional[int] = None,
        is_admin: bool = False,
    ) -> Tuple[Optional[str], str, int]:
        """
        Pause or resume notifications for a specific feed or 'all' feeds.
        Returns (feed_label, status_code, count_affected)
        status_code: 'success', 'already_paused', 'already_resumed', 'not_found', 'permission_denied'
        """
        target_id = str(target_id)
        platform = platform.lower()
        feed_identifier = feed_identifier.strip()
        is_private = (user_id is not None) and (str(target_id) == str(user_id))

        if feed_identifier.lower() == "all":
            query: Dict[str, Any] = {"platform": platform}
            if is_private or not is_admin:
                if user_id is not None:
                    query["user_id"] = user_id
                else:
                    query["target_id"] = target_id
            else:
                query["target_id"] = target_id
                if thread_id is not None:
                    query["thread_id"] = thread_id

            total = await self.db.subscriptions.count_documents(query)
            if total == 0:
                return None, "not_found", 0

            update_query = dict(query)
            update_query["is_paused"] = {"$ne": paused}
            res = await self.db.subscriptions.update_many(
                update_query, {"$set": {"is_paused": paused}}
            )
            if res.modified_count == 0:
                return None, "already_paused" if paused else "already_resumed", 0

            return None, "success", res.modified_count

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
            return None, "not_found", 0

        feed_id = feed_doc["feed_id"]
        feed_label = feed_doc.get("title") or feed_doc.get("url")

        base_query: Dict[str, Any] = {
            "feed_id": feed_id,
            "platform": platform,
            "target_id": target_id,
        }
        if thread_id is not None:
            base_query["thread_id"] = thread_id

        existing_subs = await self.db.subscriptions.find(base_query).to_list(length=100)
        if not existing_subs:
            if is_private and user_id is not None:
                user_subs = await self.db.subscriptions.find(
                    {"feed_id": feed_id, "platform": platform, "user_id": user_id}
                ).to_list(length=10)
                if user_subs:
                    existing_subs = user_subs
            if not existing_subs:
                return None, "not_found", 0

        if not is_admin and not is_private and user_id is not None:
            user_sub = next(
                (s for s in existing_subs if s.get("user_id") == user_id), None
            )
            if not user_sub:
                return feed_label, "permission_denied", 0
            target_sub = user_sub
        else:
            if user_id is not None:
                user_sub = next(
                    (s for s in existing_subs if s.get("user_id") == user_id), None
                )
                target_sub = user_sub if user_sub else existing_subs[0]
            else:
                target_sub = existing_subs[0]

        currently_paused = target_sub.get("is_paused", False)
        if currently_paused == paused:
            return feed_label, "already_paused" if paused else "already_resumed", 0

        await self.db.subscriptions.update_one(
            {"_id": target_sub["_id"]}, {"$set": {"is_paused": paused}}
        )
        return feed_label, "success", 1

    async def edit_subscription(
        self,
        platform: str,
        target_id: str,
        feed_identifier: str,
        title: Optional[str] = None,
        filter_keywords: Optional[str] = None,
        filter_exclude: Optional[str] = None,
        new_url: Optional[str] = None,
        case_sensitive: Optional[bool] = None,
        thread_id: Optional[int] = None,
        user_id: Optional[int] = None,
        is_admin: bool = False,
    ) -> Tuple[Optional[str], str, Dict[str, Any]]:
        """
        Edit a subscription in-place: title, include/exclude filters, or feed URL.
        Returns (feed_label, status_code, changed_summary).
        status_code: 'success', 'not_found', 'permission_denied', 'no_changes'
        """
        target_id = str(target_id)
        platform = platform.lower()
        feed_identifier = feed_identifier.strip()
        is_private = (user_id is not None) and (str(target_id) == str(user_id))

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
            return None, "not_found", {}

        feed_id = feed_doc["feed_id"]
        feed_label = feed_doc.get("title") or feed_doc.get("url")

        base_query: Dict[str, Any] = {
            "feed_id": feed_id,
            "platform": platform,
            "target_id": target_id,
        }
        if thread_id is not None:
            base_query["thread_id"] = thread_id

        existing_subs = await self.db.subscriptions.find(base_query).to_list(length=100)
        if not existing_subs:
            if is_private and user_id is not None:
                user_subs = await self.db.subscriptions.find(
                    {"feed_id": feed_id, "platform": platform, "user_id": user_id}
                ).to_list(length=10)
                if user_subs:
                    existing_subs = user_subs
            if not existing_subs:
                return None, "not_found", {}

        if not is_admin and not is_private and user_id is not None:
            user_sub = next(
                (s for s in existing_subs if s.get("user_id") == user_id), None
            )
            if not user_sub:
                return feed_label, "permission_denied", {}
            target_sub = user_sub
        else:
            if user_id is not None:
                user_sub = next(
                    (s for s in existing_subs if s.get("user_id") == user_id), None
                )
                target_sub = user_sub if user_sub else existing_subs[0]
            else:
                target_sub = existing_subs[0]

        update_fields: Dict[str, Any] = {}
        changed_summary: Dict[str, Any] = {}

        if title is not None:
            clean_title = title.strip()
            if clean_title.lower() in ("none", "clear", "default"):
                update_fields["custom_title"] = None
                changed_summary["title"] = "Reset to feed default"
            else:
                update_fields["custom_title"] = clean_title
                changed_summary["title"] = clean_title

        if filter_keywords is not None:
            clean_inc = filter_keywords.strip()
            if clean_inc.lower() in ("none", "clear"):
                update_fields["filter_keywords"] = None
                changed_summary["filter_keywords"] = "Cleared"
            else:
                update_fields["filter_keywords"] = clean_inc
                changed_summary["filter_keywords"] = clean_inc

        if filter_exclude is not None:
            clean_exc = filter_exclude.strip()
            if clean_exc.lower() in ("none", "clear"):
                update_fields["filter_exclude"] = None
                changed_summary["filter_exclude"] = "Cleared"
            else:
                update_fields["filter_exclude"] = clean_exc
                changed_summary["filter_exclude"] = clean_exc

        if case_sensitive is not None:
            update_fields["case_sensitive"] = case_sensitive
            changed_summary["case_sensitive"] = (
                "Enabled (strict casing)"
                if case_sensitive
                else "Disabled (case-insensitive)"
            )

        if (
            new_url is not None
            and new_url.strip()
            and new_url.strip() != feed_doc["url"]
        ):
            clean_new_url = new_url.strip()
            new_feed_id, new_feed_title = await self.add_or_get_feed(clean_new_url)
            update_fields["feed_id"] = new_feed_id
            changed_summary["url"] = clean_new_url

            old_sub_count = await self.db.subscriptions.count_documents(
                {"feed_id": feed_id, "_id": {"$ne": target_sub["_id"]}}
            )
            if old_sub_count == 0:
                await self.db.feeds.update_one(
                    {"feed_id": feed_id}, {"$set": {"is_active": False}}
                )
            await self.db.feeds.update_one(
                {"feed_id": new_feed_id}, {"$set": {"is_active": True}}
            )

        if not update_fields:
            return feed_label, "no_changes", {}

        await self.db.subscriptions.update_one(
            {"_id": target_sub["_id"]}, {"$set": update_fields}
        )
        return feed_label, "success", changed_summary

    async def list_subscriptions(
        self,
        platform: str,
        target_id: str,
        thread_id: Optional[int] = None,
        user_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """List feeds subscribed by chat, topic, and optionally filtered by user_id."""
        target_id = str(target_id)
        platform = platform.lower()

        match_cond: Dict[str, Any] = {"platform": platform, "target_id": target_id}
        if thread_id is not None:
            match_cond["thread_id"] = thread_id

        if user_id is not None:
            if str(target_id) == str(user_id):
                # In user's private DM, include their subscriptions even if user_id is legacy/null
                match_cond["$or"] = [
                    {"user_id": user_id},
                    {"user_id": None},
                    {"user_id": {"$exists": False}},
                ]
            else:
                match_cond["user_id"] = user_id

        pipeline = [
            {"$match": match_cond},
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
                    "title": {"$ifNull": ["$custom_title", "$feed_info.title"]},
                    "custom_title": "$custom_title",
                    "last_checked": "$feed_info.last_checked",
                    "filter_keywords": "$filter_keywords",
                    "filter_exclude": "$filter_exclude",
                    "thread_id": "$thread_id",
                    "user_id": "$user_id",
                    "is_paused": "$is_paused",
                    "case_sensitive": "$case_sensitive",
                    "created_at": "$created_at",
                }
            },
            {"$sort": {"feed_id": ASCENDING}},
        ]

        cursor = self.db.subscriptions.aggregate(pipeline)
        return await cursor.to_list(length=500)

    async def list_user_all_subscriptions(self, user_id: int) -> List[Dict[str, Any]]:
        """List all subscriptions across all platforms and targets for a specific user."""
        if not user_id:
            return []
        pipeline = [
            {"$match": {"user_id": user_id}},
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
                    "title": {"$ifNull": ["$custom_title", "$feed_info.title"]},
                    "custom_title": "$custom_title",
                    "platform": "$platform",
                    "target_id": "$target_id",
                    "thread_id": "$thread_id",
                    "filter_keywords": "$filter_keywords",
                    "filter_exclude": "$filter_exclude",
                    "last_checked": "$feed_info.last_checked",
                    "is_paused": "$is_paused",
                    "case_sensitive": "$case_sensitive",
                    "created_at": "$created_at",
                }
            },
            {"$sort": {"feed_id": ASCENDING}},
        ]
        cursor = self.db.subscriptions.aggregate(pipeline)
        return await cursor.to_list(length=500)

    async def count_user_subscriptions(self, user_id: int) -> int:
        """Count total active subscriptions added by a specific user across all chats."""
        if not user_id:
            return 0
        return await self.db.subscriptions.count_documents({"user_id": user_id})

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
        """Get all subscriber targets (Telegram / Discord) for a given feed, excluding paused ones."""
        cursor = self.db.subscriptions.find(
            {"feed_id": feed_id, "is_paused": {"$ne": True}}
        )
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

    async def is_media_alerted(
        self,
        platform: str,
        target_id: str,
        canonical_key: Optional[str],
        thread_id: Optional[int] = None,
    ) -> bool:
        """Check if this canonical media release has already been alerted to this destination."""
        if not canonical_key:
            return False
        doc = await self.db.alerted_media.find_one(
            {
                "platform": platform.lower(),
                "target_id": str(target_id),
                "thread_id": thread_id,
                "canonical_key": canonical_key,
            }
        )
        return doc is not None

    async def record_media_alerted(
        self,
        platform: str,
        target_id: str,
        canonical_key: Optional[str],
        title: str,
        thread_id: Optional[int] = None,
    ) -> None:
        """Record a canonical media release as alerted to a destination to prevent cross-group duplicates."""
        if not canonical_key:
            return
        doc = {
            "platform": platform.lower(),
            "target_id": str(target_id),
            "thread_id": thread_id,
            "canonical_key": canonical_key,
            "title": title,
            "alerted_at": datetime.now(timezone.utc),
        }
        try:
            await self.db.alerted_media.insert_one(doc)
        except DuplicateKeyError:
            pass
