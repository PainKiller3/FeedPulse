import asyncio
import logging
import time
from typing import Optional, List, Dict, Any
import aiohttp

from database.db import Database
from parser.feed_parser import FeedParser, FeedItem
from services.dispatcher import NotificationDispatcher

try:
    from config import (
        POLL_INTERVAL_SECONDS,
        INITIAL_ITEMS_TO_ALERT,
        MAX_ITEMS_PER_CHECK,
    )
except ImportError:
    from config_sample import (
        POLL_INTERVAL_SECONDS,
        INITIAL_ITEMS_TO_ALERT,
        MAX_ITEMS_PER_CHECK,
    )

logger = logging.getLogger(__name__)


class FeedPoller:
    """Background engine that periodically fetches feeds and dispatches alerts for new items."""

    def __init__(
        self,
        db: Database,
        dispatcher: NotificationDispatcher,
        smart_title_deduplication: bool = False,
    ):
        self.db = db
        self.dispatcher = dispatcher
        self.smart_title_deduplication = smart_title_deduplication
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._manual_trigger_event = asyncio.Event()
        self._last_cleanup_time = 0.0

    @property
    def poll_interval(self) -> int:
        return POLL_INTERVAL_SECONDS

    @property
    def interval_seconds(self) -> int:
        return POLL_INTERVAL_SECONDS

    async def start(self) -> None:
        """Start the background polling loop."""
        if self._running:
            return
        self._running = True
        self._session = aiohttp.ClientSession()
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "FeedPoller started with check interval of %ds", POLL_INTERVAL_SECONDS
        )

    async def stop(self) -> None:
        """Stop the background polling loop."""
        self._running = False
        if self._manual_trigger_event:
            self._manual_trigger_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("FeedPoller stopped")

    def trigger_immediate_check(self) -> None:
        """Signal the poller to run an immediate check."""
        self._manual_trigger_event.set()

    async def _run_loop(self) -> None:
        """Main polling loop."""
        while self._running:
            try:
                await self.poll_all_feeds()
            except Exception as e:
                logger.error(
                    "Unexpected error during feed polling cycle: %s", e, exc_info=True
                )

            # Daily cleanup of seen items older than 14 days
            now = time.time()
            if now - self._last_cleanup_time > 86400:
                try:
                    purged = await self.db.cleanup_old_seen_items(days=14)
                    logger.info("Cleaned up %d old seen items from database", purged)
                    self._last_cleanup_time = now
                except Exception as e:
                    logger.warning("Error running database cleanup: %s", e)

            # Wait for next interval or manual trigger event
            try:
                await asyncio.wait_for(
                    self._manual_trigger_event.wait(), timeout=POLL_INTERVAL_SECONDS
                )
                self._manual_trigger_event.clear()
                logger.info("Manual check triggered")
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                break

    async def poll_all_feeds(self) -> None:
        """Fetch and process all active subscribed feeds."""
        active_feeds = await self.db.get_active_feeds()
        if not active_feeds:
            logger.debug("No active feeds to poll")
            return

        logger.info("Polling %d active feeds...", len(active_feeds))
        for feed in active_feeds:
            if not self._running:
                break
            try:
                await self.process_feed(feed)
            except Exception as e:
                logger.error(
                    "Error processing feed %s (%s): %s", feed["id"], feed["url"], e
                )

    async def process_feed(self, feed: Dict[str, Any]) -> int:
        """
        Check a single feed for new items and dispatch alerts.
        Returns the number of new items alerted.
        """
        feed_id = feed["id"]
        url = feed["url"]
        etag = feed.get("etag")
        modified = feed.get("modified")

        if not self._session:
            self._session = aiohttp.ClientSession()

        title, new_etag, new_modified, items, status = await FeedParser.fetch_and_parse(
            self._session, url, etag=etag, modified=modified
        )

        if status == 304:
            # Not modified
            await self.db.update_feed_checked(feed_id)
            return 0

        if status != 200 or not items:
            logger.debug("Feed %s returned status %s or no items", url, status)
            await self.db.update_feed_checked(feed_id)
            return 0

        effective_title = title or feed.get("title") or url
        await self.db.update_feed_checked(
            feed_id, title=effective_title, etag=new_etag, modified=new_modified
        )

        # Check if this feed has seen items before
        has_history = await self.db.has_seen_any_items(feed_id)

        if not has_history:
            # First time polling this feed
            # Mark all existing items as seen so we don't spam the user with 50 old torrents
            logger.info(
                "Feed %s (%s) first poll: recording %d baseline items",
                feed_id,
                effective_title,
                len(items),
            )

            baseline_items = [(it.item_key, it.title) for it in items]
            await self.db.mark_items_seen(feed_id, baseline_items)

            # Optionally alert the single newest item if INITIAL_ITEMS_TO_ALERT > 0
            if INITIAL_ITEMS_TO_ALERT > 0 and items:
                newest = items[:INITIAL_ITEMS_TO_ALERT]
                subscribers = await self.db.get_subscribers_for_feed(feed_id)
                for item in newest:
                    await self._notify_subscribers(subscribers, item, effective_title)
                return len(newest)

            return 0

        # Feed has history: identify unseen items
        new_items: List[FeedItem] = []
        for item in items:
            is_seen = await self.db.is_item_seen(feed_id, item.item_key)
            if not is_seen:
                new_items.append(item)

        if not new_items:
            logger.debug("No new items for feed %s (%s)", feed_id, effective_title)
            return 0

        # Protect against flood if a feed suddenly returns 100 new items
        items_to_alert = new_items[:MAX_ITEMS_PER_CHECK]
        logger.info(
            "Found %d new items for feed %s (%s); alerting %d",
            len(new_items),
            feed_id,
            effective_title,
            len(items_to_alert),
        )

        subscribers = await self.db.get_subscribers_for_feed(feed_id)
        if not subscribers:
            logger.debug(
                "No unpaused subscribers found for active feed %s (%s)",
                feed_id,
                effective_title,
            )
        else:
            # Reverse items so oldest new item is sent first (chronological order)
            for item in reversed(items_to_alert):
                await self._notify_subscribers(subscribers, item, effective_title)
                # Short sleep to prevent hitting Telegram/Discord rate limits
                await asyncio.sleep(0.1)

        # Mark all new items as seen
        items_to_mark = [(it.item_key, it.title) for it in new_items]
        await self.db.mark_items_seen(feed_id, items_to_mark)

        return len(items_to_alert)

    async def _notify_subscribers(
        self, subscribers: List[Dict[str, Any]], item: FeedItem, feed_title: str
    ) -> None:
        """Dispatch item to all subscribers matching keyword filters, deduplicated per destination."""
        sent_destinations = set()

        for sub in subscribers:
            platform = sub["platform"]
            target_id = sub["target_id"]
            thread_id = sub.get("thread_id")
            filter_keywords = sub.get("filter_keywords")
            filter_exclude = sub.get("filter_exclude")

            # Check keyword filtering
            case_sensitive = sub.get("case_sensitive", False)
            if not FeedParser.matches_filters(
                item.title,
                filter_keywords,
                filter_exclude,
                case_sensitive=case_sensitive,
            ):
                logger.debug(
                    "Item '%s' filtered out for subscriber %s:%s (inc=%s, exc=%s)",
                    item.title,
                    platform,
                    target_id,
                    filter_keywords,
                    filter_exclude,
                )
                continue

            # Check size limit filtering
            max_size = sub.get("max_size")
            min_size = sub.get("min_size")
            if not FeedParser.matches_size_filter(
                item.size_bytes, min_size=min_size, max_size=max_size
            ):
                logger.debug(
                    "Item '%s' (%s bytes) filtered out by size limits (min=%s, max=%s) for subscriber %s:%s",
                    item.title,
                    item.size_bytes,
                    min_size,
                    max_size,
                    platform,
                    target_id,
                )
                continue

            # Prevent duplicate alerts to the same chat/topic when multiple users subscribe to the same feed
            dest_key = (platform, target_id, thread_id)
            if dest_key in sent_destinations:
                continue

            # Smart cross-group title deduplication (e.g. Immortals vs PSA for same movie & resolution)
            if self.smart_title_deduplication and item.canonical_media_key:
                already_alerted = await self.db.is_media_alerted(
                    platform, target_id, item.canonical_media_key, thread_id=thread_id
                )
                if already_alerted:
                    logger.info(
                        "Skipping cross-group duplicate for %s:%s (thread: %s) — '%s' already alerted ('%s')",
                        platform,
                        target_id,
                        thread_id,
                        item.canonical_media_key,
                        item.title,
                    )
                    continue

            try:
                success = await self.dispatcher.dispatch(
                    platform, target_id, item, feed_title, thread_id=thread_id
                )
                if success:
                    sent_destinations.add(dest_key)
                    if self.smart_title_deduplication and item.canonical_media_key:
                        await self.db.record_media_alerted(
                            platform,
                            target_id,
                            item.canonical_media_key,
                            item.title,
                            thread_id=thread_id,
                        )
            except Exception as e:
                logger.error(
                    "Failed to dispatch alert to %s:%s (thread: %s): %s",
                    platform,
                    target_id,
                    thread_id,
                    e,
                )
