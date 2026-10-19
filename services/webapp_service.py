"""
FeedPulse - Telegram Mini App & Web Server Service
Provides high-performance async REST API and static asset hosting for the FeedPulse Telegram Web App.
Built using aiohttp for seamless asyncio event loop integration alongside the Telegram & Discord bots.
"""

from datetime import date, datetime
import hashlib
import hmac
import json
import logging
import os
import urllib.parse
from typing import Any, Dict, List, Optional, Set

import aiohttp
from aiohttp import web

from database.db import Database
from parser.feed_parser import FeedParser, format_bytes
from services.dispatcher import NotificationDispatcher
from services.poller import FeedPoller

try:
    from config import POLL_INTERVAL_SECONDS
except ImportError:
    from config_sample import POLL_INTERVAL_SECONDS

logger = logging.getLogger("FeedPulse.WebApp")


def safe_json_dumps(obj: Any) -> str:
    """Serialize objects to JSON safely, handling datetimes and non-standard types."""

    def default_serializer(o: Any) -> Any:
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        return str(o)

    return json.dumps(obj, default=default_serializer)


def safe_json_response(data: Any, status: int = 200) -> web.Response:
    """Helper to return JSON response with automatic datetime and ObjectId serialization."""
    return web.json_response(data, status=status, dumps=safe_json_dumps)


def validate_telegram_init_data(
    init_data_raw: str, bot_token: str
) -> Optional[Dict[str, Any]]:
    """
    Validate Telegram Mini App cryptographic signature (HMAC-SHA256).
    Returns the parsed user dict if valid, otherwise None.
    """
    if not init_data_raw or not bot_token:
        return None

    try:
        parsed = dict(urllib.parse.parse_qsl(init_data_raw, keep_blank_values=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None

        # Build data-check-string (alphabetically sorted key=value separated by \n)
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))

        # secret_key = HMAC_SHA256(key="WebAppData", data=bot_token)
        secret_key = hmac.new(
            b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256
        ).digest()

        # computed_hash = HMAC_SHA256(key=secret_key, data=data_check_string)
        computed_hash = hmac.new(
            secret_key, data_check_string.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        if hmac.compare_digest(computed_hash, received_hash):
            user_json = parsed.get("user")
            if user_json:
                return json.loads(user_json)
            return {"id": int(parsed.get("query_id", 0))}
    except Exception as e:
        logger.warning("Telegram WebApp initData validation error: %s", e)
    return None


class WebAppServer:
    """Embedded Web Server hosting the FeedPulse Telegram Mini App & REST API."""

    def __init__(
        self,
        db: Database,
        poller: FeedPoller,
        dispatcher: NotificationDispatcher,
        bot_token: str = "",
        owner_id: int = 0,
        authorized_chats: Optional[List[int]] = None,
        sudo_users: Optional[List[int]] = None,
        host: str = "0.0.0.0",
        port: int = 8080,
        webapp_url: str = "",
        telegram_bot_service: Optional[Any] = None,
    ):
        self.db = db
        self.poller = poller
        self.dispatcher = dispatcher
        self.bot_token = bot_token
        self.owner_id = owner_id or 0
        self.authorized_chats: Set[int] = set(authorized_chats or [])
        self.sudo_users: Set[int] = set(sudo_users or [])
        self.host = host
        self.port = port
        self.webapp_url = webapp_url
        self.telegram_bot = telegram_bot_service
        self.app: Optional[web.Application] = None
        self.runner: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None
        self._session: Optional[aiohttp.ClientSession] = None

    def is_super_user(self, user_id: int) -> bool:
        """Check if user is Bot Owner or Sudo."""
        if not user_id:
            return False
        return (self.owner_id and user_id == self.owner_id) or (
            user_id in self.sudo_users
        )

    async def get_request_user(self, request: web.Request) -> Dict[str, Any]:
        """
        Extract authenticated user from request:
        1. Checks 'Authorization: tma <initData>' or 'X-Telegram-Init-Data' header.
        2. In browser fallback/dev: supports 'X-User-Id' header or '?user_id=...' query param.
        """
        auth_header = request.headers.get("Authorization", "")
        init_data = ""
        if auth_header.startswith("tma "):
            init_data = auth_header[4:].strip()
        elif "X-Telegram-Init-Data" in request.headers:
            init_data = request.headers["X-Telegram-Init-Data"].strip()

        if init_data:
            user_data = validate_telegram_init_data(init_data, self.bot_token)
            if user_data:
                uid = int(user_data.get("id", 0))
                return {
                    "id": uid,
                    "first_name": user_data.get("first_name", "User"),
                    "last_name": user_data.get("last_name", ""),
                    "username": user_data.get("username", ""),
                    "authenticated": True,
                    "is_super": self.is_super_user(uid),
                }

        # Standalone Browser fallback (for preview/testing outside Telegram)
        user_id_param = request.headers.get("X-User-Id") or request.query.get("user_id")
        if user_id_param and str(user_id_param).lstrip("-").isdigit():
            uid = int(user_id_param)
            return {
                "id": uid,
                "first_name": "Web User",
                "last_name": "",
                "username": f"user_{uid}",
                "authenticated": False,  # unverified fallback
                "is_super": self.is_super_user(uid),
            }

        # Guest default
        guest_id = self.owner_id if self.owner_id else 10001
        return {
            "id": guest_id,
            "first_name": "Preview Mode",
            "last_name": "",
            "username": "guest",
            "authenticated": False,
            "is_super": self.is_super_user(guest_id),
        }

    # ---------------- Route Handlers ----------------

    async def handle_index(self, request: web.Request) -> web.Response:
        """Serve main SPA HTML."""
        webapp_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "webapp")
        index_path = os.path.join(webapp_dir, "index.html")
        if os.path.exists(index_path):
            return web.FileResponse(index_path)
        return web.Response(
            text="<h1>FeedPulse Web App</h1><p>Frontend assets are loading...</p>",
            content_type="text/html",
        )

    async def handle_api_me(self, request: web.Request) -> web.Response:
        """Return current user profile, role, feed counts, and permitted destinations."""
        user = await self.get_request_user(request)
        uid = user["id"]

        is_owner = bool(self.owner_id and uid == self.owner_id)
        is_sudo = uid in self.sudo_users
        role = "Bot Owner" if is_owner else ("Sudo User" if is_sudo else "User")

        # Destination list available to user
        destinations = [
            {
                "id": str(uid),
                "title": f"Personal DM ({user.get('first_name', 'Private')})",
                "type": "private",
            }
        ]

        # Add authorized chats if accessible
        for cid in self.authorized_chats:
            destinations.append(
                {
                    "id": str(cid),
                    "title": f"Authorized Group ({cid})",
                    "type": "group",
                }
            )

        # Count feeds
        user_subs = await self.db.list_user_all_subscriptions(uid)
        active_feeds = await self.db.get_active_feeds()

        return safe_json_response(
            {
                "user": user,
                "role": role,
                "is_super": user["is_super"],
                "stats": {
                    "my_feeds_count": len(user_subs),
                    "total_system_feeds": len(active_feeds),
                    "poll_interval_seconds": getattr(
                        self.poller, "poll_interval", POLL_INTERVAL_SECONDS
                    ),
                    "is_rich_text": self.dispatcher.is_rich_text_enabled(str(uid)),
                },
                "destinations": destinations,
            }
        )

    async def handle_api_feeds(self, request: web.Request) -> web.Response:
        """List active feeds for authenticated user or specific target."""
        user = await self.get_request_user(request)
        uid = user["id"]
        target = request.query.get("target_id")

        if target:
            subs = await self.db.list_subscriptions("telegram", str(target))
        elif user["is_super"]:
            # Super users can see all user subscriptions
            subs = await self.db.list_user_all_subscriptions(uid)
        else:
            subs = await self.db.list_user_all_subscriptions(uid)

        # Clean ObjectId from MongoDB
        cleaned = []
        for s in subs:
            item = dict(s)
            if "_id" in item:
                item["_id"] = str(item["_id"])
            cleaned.append(item)

        return safe_json_response({"feeds": cleaned, "count": len(cleaned)})

    async def handle_api_add_feed(self, request: web.Request) -> web.Response:
        """Subscribe to a new feed with filters and destination."""
        user = await self.get_request_user(request)
        uid = user["id"]

        try:
            data = await request.json()
        except Exception:
            return safe_json_response({"error": "Invalid JSON body"}, status=400)

        url = (data.get("url") or "").strip()
        title = (data.get("title") or "").strip()
        target_id = str(data.get("target_id") or uid).strip()
        thread_id = data.get("thread_id")
        if thread_id is not None:
            try:
                thread_id = int(thread_id)
            except ValueError:
                thread_id = None

        inc_filter = (data.get("filter_keywords") or "").strip() or None
        exc_filter = (data.get("filter_exclude") or "").strip() or None
        case_sensitive = bool(data.get("case_sensitive", False))
        max_size = data.get("max_size")
        min_size = data.get("min_size")

        if not url:
            return safe_json_response({"error": "Feed URL is required"}, status=400)

        # Pre-validate RSS feed
        try:
            feed_title, _, _, items, status_code = await FeedParser.fetch_and_parse(
                self._session, url
            )
            if status_code != 200 and not items:
                return safe_json_response(
                    {
                        "error": f"Could not fetch feed (HTTP {status_code}). Please verify URL."
                    },
                    status=400,
                )
            if not title and feed_title:
                title = feed_title
        except Exception as e:
            logger.warning("Feed validation warning for %s: %s", url, e)

        try:
            feed_id, feed_title, is_new = await self.db.subscribe(
                platform="telegram",
                target_id=target_id,
                url=url,
                title=title or url,
                filter_keywords=inc_filter,
                filter_exclude=exc_filter,
                thread_id=thread_id,
                user_id=uid,
                case_sensitive=case_sensitive,
                max_size=int(max_size) if max_size else None,
                min_size=int(min_size) if min_size else None,
            )
            # Trigger background poll cycle
            self.poller.trigger_immediate_check()

            return safe_json_response(
                {
                    "success": True,
                    "feed_id": feed_id,
                    "title": feed_title or title or url,
                    "is_new": is_new,
                    "message": "Feed subscribed successfully!",
                }
            )
        except Exception as e:
            logger.error("Failed to add feed via WebApp: %s", e)
            return safe_json_response({"error": str(e)}, status=500)

    async def handle_api_pause_feed(self, request: web.Request) -> web.Response:
        """Pause or resume a feed."""
        feed_id = request.match_info.get("feed_id", "")
        user = await self.get_request_user(request)
        uid = user["id"]
        is_admin = user["is_super"]

        try:
            data = await request.json()
            is_paused = bool(data.get("paused", True))
            target_id = data.get("target_id")
        except Exception:
            is_paused = True
            target_id = None

        if not target_id:
            target_id = request.query.get("target_id")

        if not target_id and feed_id.isdigit():
            # Auto-resolve target_id from user's active subscription
            sub_doc = await self.db.subscriptions.find_one(
                {"feed_id": int(feed_id), "user_id": uid}
            )
            if sub_doc:
                target_id = str(sub_doc.get("target_id") or "")
        if not target_id:
            target_id = str(uid)

        try:
            feed_label, status_code, count = await self.db.set_subscription_pause(
                platform="telegram",
                target_id=str(target_id),
                feed_identifier=feed_id,
                paused=is_paused,
                user_id=uid,
                is_admin=is_admin,
            )
            if status_code in ("success", "already_paused", "already_resumed"):
                return safe_json_response(
                    {
                        "success": True,
                        "feed_id": feed_id,
                        "is_paused": is_paused,
                        "title": feed_label or feed_id,
                        "affected": count,
                    }
                )
            return safe_json_response(
                {"error": status_code or "Feed not found or permission denied"},
                status=404 if status_code == "not_found" else 403,
            )
        except Exception as e:
            logger.error("Error pausing feed %s via WebApp: %s", feed_id, e)
            return safe_json_response({"error": str(e)}, status=500)

    async def handle_api_delete_feed(self, request: web.Request) -> web.Response:
        """Unsubscribe from a feed."""
        feed_id = request.match_info.get("feed_id", "")
        user = await self.get_request_user(request)
        uid = user["id"]
        is_admin = user["is_super"]
        target_id = request.query.get("target_id")

        if not target_id and feed_id.isdigit():
            # Auto-resolve target_id from user's active subscription
            sub_doc = await self.db.subscriptions.find_one(
                {"feed_id": int(feed_id), "user_id": uid}
            )
            if sub_doc:
                target_id = str(sub_doc.get("target_id") or "")
        if not target_id:
            target_id = str(uid)

        try:
            feed_label, status_code = await self.db.unsubscribe(
                platform="telegram",
                target_id=str(target_id),
                feed_identifier=feed_id,
                user_id=uid,
                is_admin=is_admin,
            )
            if status_code == "success":
                return safe_json_response(
                    {
                        "success": True,
                        "feed_id": feed_id,
                        "message": f"Successfully unsubscribed from {feed_label or feed_id}",
                    }
                )
            return safe_json_response(
                {"error": status_code or "Feed not found or permission denied"},
                status=404 if status_code == "not_found" else 403,
            )
        except Exception as e:
            logger.error("Error deleting feed %s via WebApp: %s", feed_id, e)
            return safe_json_response({"error": str(e)}, status=500)

    async def handle_api_edit_feed(self, request: web.Request) -> web.Response:
        """Edit feed title, filters, or URL in-place."""
        feed_id = request.match_info.get("feed_id", "")
        user = await self.get_request_user(request)
        uid = user["id"]
        is_admin = user["is_super"]

        try:
            data = await request.json()
        except Exception:
            return safe_json_response({"error": "Invalid JSON body"}, status=400)

        title = data.get("title")
        inc_filter = data.get("filter_keywords")
        exc_filter = data.get("filter_exclude")
        new_url = data.get("url")
        case_sensitive = data.get("case_sensitive")
        max_size = data.get("max_size")
        min_size = data.get("min_size")
        target_id = str(data.get("target_id") or "")

        if not target_id and feed_id.isdigit():
            # Auto-resolve target_id from user's active subscription
            sub_doc = await self.db.subscriptions.find_one(
                {"feed_id": int(feed_id), "user_id": uid}
            )
            if sub_doc:
                target_id = str(sub_doc.get("target_id") or "")
        if not target_id:
            target_id = str(uid)

        try:
            feed_label, status, changed = await self.db.edit_subscription(
                platform="telegram",
                target_id=target_id,
                feed_identifier=feed_id,
                title=title,
                filter_keywords=inc_filter,
                filter_exclude=exc_filter,
                new_url=new_url,
                case_sensitive=case_sensitive,
                max_size=int(max_size) if max_size is not None else None,
                min_size=int(min_size) if min_size is not None else None,
                user_id=uid,
                is_admin=is_admin,
            )
            if status in ("success", "no_changes"):
                return safe_json_response(
                    {
                        "success": True,
                        "feed_id": feed_id,
                        "title": feed_label,
                        "changed": changed,
                    }
                )
            return safe_json_response(
                {"error": status}, status=404 if status == "not_found" else 400
            )
        except Exception as e:
            logger.error("Error editing feed %s via WebApp: %s", feed_id, e)
            return safe_json_response({"error": str(e)}, status=500)

    async def handle_api_inspect_feed(self, request: web.Request) -> web.Response:
        """Inspect live feed items from an active feed ID or test arbitrary URL."""
        feed_id = request.match_info.get("feed_id")
        url = request.query.get("url")

        if not url and feed_id:
            feed_doc = await self.db.get_feed_by_identifier(feed_id)
            if feed_doc:
                url = feed_doc.get("url")

        if not url:
            return safe_json_response(
                {"error": "Feed URL or valid feed_id is required"}, status=400
            )

        try:
            feed_title, _, _, items, status_code = await FeedParser.fetch_and_parse(
                self._session, url
            )
            parsed_items = []
            for item in items[:20]:  # Limit preview to 20 latest items
                parsed_items.append(
                    {
                        "title": item.title,
                        "link": item.link,
                        "published": item.published,
                        "file_size": item.size_str
                        or (format_bytes(item.size_bytes) if item.size_bytes else None),
                        "size_bytes": item.size_bytes,
                        "seeds": item.seeds,
                        "leechers": item.leechers,
                        "category": item.category,
                        "magnet_uri": item.magnet_uri,
                        "torrent_url": item.torrent_url,
                        "description": (item.description or "")[:300],
                    }
                )
            return safe_json_response(
                {
                    "url": url,
                    "title": feed_title or url,
                    "status_code": status_code,
                    "total_items": len(items),
                    "items": parsed_items,
                }
            )
        except Exception as e:
            logger.error("Live inspect error for %s: %s", url, e)
            return safe_json_response({"error": str(e)}, status=500)

    async def handle_api_system_check(self, request: web.Request) -> web.Response:
        """Trigger an immediate feed poll check cycle."""
        self.poller.trigger_immediate_check()
        return safe_json_response(
            {
                "success": True,
                "message": "Immediate feed check cycle triggered!",
            }
        )

    async def handle_api_system_stats(self, request: web.Request) -> web.Response:
        """Get live system stats and diagnostics."""
        active_feeds = await self.db.get_active_feeds()
        return safe_json_response(
            {
                "status": "online",
                "active_feeds_count": len(active_feeds),
                "poll_interval_seconds": getattr(
                    self.poller, "poll_interval", POLL_INTERVAL_SECONDS
                ),
                "smart_deduplication": bool(
                    getattr(self.poller, "smart_title_deduplication", False)
                ),
            }
        )

    async def handle_api_toggle_richtext(self, request: web.Request) -> web.Response:
        """Toggle rich text formatting for a specific chat or user."""
        user = await self.get_request_user(request)
        uid = str(user["id"])
        try:
            data = await request.json()
            target_id = str(data.get("target_id") or uid)
            enabled = data.get("enabled")
        except Exception:
            target_id = uid
            enabled = None

        if enabled is None:
            cur = self.dispatcher.is_rich_text_enabled(target_id)
            enabled = not cur

        await self.db.set_chat_setting(target_id, "rich_text", enabled)
        self.dispatcher.set_rich_text(target_id, enabled)
        return safe_json_response(
            {"target_id": target_id, "rich_text_enabled": enabled}
        )

    # ---------------- Lifecycle ----------------

    async def start(self) -> None:
        """Initialize aiohttp Web Application and start listening."""
        self._session = aiohttp.ClientSession()
        self.app = web.Application()

        # Static assets
        webapp_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "webapp")
        css_dir = os.path.join(webapp_dir, "css")
        js_dir = os.path.join(webapp_dir, "js")
        assets_dir = os.path.join(webapp_dir, "assets")

        # API Routes
        self.app.router.add_get("/", self.handle_index)
        self.app.router.add_get("/api/me", self.handle_api_me)
        self.app.router.add_get("/api/feeds", self.handle_api_feeds)
        self.app.router.add_post("/api/feeds/add", self.handle_api_add_feed)
        self.app.router.add_post(
            "/api/feeds/{feed_id}/pause", self.handle_api_pause_feed
        )
        self.app.router.add_post("/api/feeds/{feed_id}/edit", self.handle_api_edit_feed)
        self.app.router.add_delete("/api/feeds/{feed_id}", self.handle_api_delete_feed)
        self.app.router.add_get(
            "/api/feeds/{feed_id}/items", self.handle_api_inspect_feed
        )
        self.app.router.add_get("/api/inspect", self.handle_api_inspect_feed)
        self.app.router.add_post("/api/system/check", self.handle_api_system_check)
        self.app.router.add_get("/api/system/stats", self.handle_api_system_stats)
        self.app.router.add_post(
            "/api/settings/richtext", self.handle_api_toggle_richtext
        )

        # Serve static directories
        if os.path.exists(css_dir):
            self.app.router.add_static("/css/", path=css_dir, name="css")
        if os.path.exists(js_dir):
            self.app.router.add_static("/js/", path=js_dir, name="js")
        if os.path.exists(assets_dir):
            self.app.router.add_static("/assets/", path=assets_dir, name="assets")

        # Catch-all for SPA client routing
        self.app.router.add_get("/{tail:.*}", self.handle_index)

        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.host, self.port)
        await self.site.start()

        logger.info(
            "⚡ FeedPulse WebApp Server running at http://%s:%d (Public URL: %s)",
            self.host,
            self.port,
            self.webapp_url or f"http://{self.host}:{self.port}",
        )

    async def stop(self) -> None:
        """Gracefully shut down web server and HTTP session."""
        logger.info("Stopping FeedPulse WebApp Server...")
        if self._session:
            await self._session.close()
        if self.runner:
            await self.runner.cleanup()
        logger.info("FeedPulse WebApp Server stopped.")
