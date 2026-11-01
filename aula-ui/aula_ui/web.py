"""Ingress-only HTTP server. Supervisor authenticates the parent session."""

from __future__ import annotations

import hashlib
import logging
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import secrets
from typing import Any
from urllib.parse import parse_qs, urlencode, quote
from .aula_api import AulaApiError, _call, fetch_thread_messages
from . import gallery
from .media import attachments, download_attachment
from .previews import PreviewCache, source_key
import requests
from .diagnostics import TRACE, correlation, describe
from . import notifications
from .access import AccessVerifier
from .mcp import MAX_BODY as MCP_MAX_BODY, McpEndpoint
from . import __version__

_LOGGER = logging.getLogger(__name__)
STATIC = Path(__file__).with_name("static")
INGRESS_IP = "172.30.32.2"
INGRESS_PATH_RE = re.compile(r"^/api/hassio_ingress/[A-Za-z0-9_-]+$")
CALENDAR_RE = re.compile(r"^/api/aula_calendar/[A-Za-z0-9_-]{20,128}\.ics$")
SPA_RE = re.compile(r"^/(post|thread|child)/[^/]+(/.*)?$|^/calendar-view(/.*)?$")
STATIC_FILE_RE = re.compile(r"^/(icons/)?[A-Za-z0-9_][A-Za-z0-9_.-]*\.(png|svg|ico|webp)$")
IMAGE_TYPES = {"png": "image/png", "svg": "image/svg+xml", "ico": "image/x-icon", "webp": "image/webp"}
# Idle or half-sent requests (slowloris, short bodies) must not pin a thread forever.
# Applies while reading the request; cleared before the response is written.
REQUEST_TIMEOUT = 60
SERVE_POLL_INTERVAL = 0.5
# Personal-data-free shell assets may be revalidated with ETags instead of re-sent.
STATIC_CACHE = "no-cache"
CSP = ("default-src 'self'; style-src 'self'; script-src 'self'; "
       "connect-src 'self'; img-src 'self' data: https:; base-uri 'self'; "
       "frame-src 'self' blob:; object-src 'self' blob:; media-src 'self' blob:; "
       "manifest-src 'self'; worker-src 'self'; frame-ancestors 'self'")


def _reading_item(controller: Any, item_id: str) -> dict | None:
    """Find a feed item for Learn more: stored posts/messages, else published items such as calendar events."""
    item = next((entry for entry in controller.store.get("feed", []) if entry.get("id") == item_id), None)
    if item is None:
        try:
            published = json.loads(controller.feed_json).get("items", [])
        except (ValueError, TypeError, AttributeError):
            published = []
        item = next((entry for entry in published if entry.get("id") == item_id), None)
    return item if item and item.get("content_type") != "Presence" else None


def start_server(controller: Any, port: int = 8099, allowed_ip: str = INGRESS_IP,
                 access: AccessVerifier | None = None) -> ThreadingHTTPServer:
    from .sickness import SickDays
    sick_days = SickDays(controller)
    cfg = getattr(controller, "config", None)
    previews = PreviewCache(cfg.data_dir) if cfg is not None and getattr(cfg, "data_dir", None) else None
    mcp = McpEndpoint(controller, sick_days, getattr(cfg, "mcp_key", ""), getattr(cfg, "mcp_allow_writes", False))
    class Handler(BaseHTTPRequestHandler):
        timeout = REQUEST_TIMEOUT
        server_version = "AulaUI"
        sys_version = ""

        def handle_one_request(self):
            token = correlation.set(uuid.uuid4().hex[:12])
            self._log_started = time.monotonic()
            self._log_status = None
            try:
                super().handle_one_request()
            finally:
                if self._log_status is not None:
                    path = getattr(self, "path", "").split("?", 1)[0]
                    known = {"/", "/account", "/calendar.ics", "/api/feed", "/api/account",
                             "/api/profiles", "/api/ai-settings", "/api/feed/reprocess", "/api/feed/reading",
                             "/api/feed/attachment", "/api/feed/media", "/api/sickness",
                             "/api/sync-settings", "/sync-settings.js",
                             "/api/notifications", "/api/notifications/test", "/api/notifications/history",
                             "/api/notifications/read", "/notifications.js",
                             "/api/push/subscribe", "/api/push/device", "/api/push/remove", "/api/push/test",
                             "/api/calendar/review", "/api/calendar/archive", "/api/calendar/rotate",
                             "/api/calendar/categories", "/api/whoami", "/api/version", "/api/feed/read", "/api/gallery/album", "/api/gallery/image", "/api/gallery/tag",
                             "/api/auth/start", "/api/auth/cancel", "/api/auth/identity", "/api/refresh",
                             "/app.js", "/account.js", "/ai-settings.js", "/style.css", "/icon.svg",
                             "/manifest.webmanifest", "/sw.js", "/pwa.js", "/theme.js", "/offline.html"}
                    route = path if path in known else (
                        "/spa" if SPA_RE.match(path) else
                        "/api/aula_calendar/:token" if path.startswith("/api/aula_calendar/") else
                        "/mcp/:key" if path.startswith("/mcp/") else
                        "/api/profiles/:child" if path.startswith("/api/profiles/") else
                        "/api/avatars/:child" if path.startswith("/api/avatars/") else "unknown")
                    level = logging.WARNING if self._log_status >= 400 else (
                        logging.INFO if getattr(self, "command", "") == "POST" else TRACE)
                    _LOGGER.log(level, "event=ingress.request method=%s route=%s status=%d duration_ms=%d",
                                getattr(self, "command", "unknown") if getattr(self, "command", "") in
                                ("GET", "POST", "HEAD", "OPTIONS") else "other",
                                route, self._log_status,
                                round((time.monotonic() - self._log_started) * 1000))
                correlation.reset(token)

        def send_response(self, code, message=None):
            self._log_status = code
            super().send_response(code, message)

        def end_headers(self):
            # Applied to every response, including send_error pages and HEAD.
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP)
            if not getattr(self, "_ingress", True):
                self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
            # The read timeout guards request parsing only; large attachments to slow
            # clients (streamed through the proxies) may legitimately take longer.
            self.connection.settimeout(None)
            super().end_headers()

        def _authorized(self) -> bool:
            # Do not trust forwarded headers: only Supervisor's ingress proxy
            # may access children's data. No host port is exposed.
            if self.client_address[0] == allowed_ip:
                self._ingress = True
                self._identity = "HA"
                return True
            self._ingress = False
            # Other sources (the cloudflared add-on) need a valid Access JWT.
            claims = access.verify(self.headers.get("Cf-Access-Jwt-Assertion")) if access is not None else None
            if claims:
                self._identity = str(claims.get("email") or claims.get("common_name") or claims.get("sub") or "")
                return True
            self.send_error(403, "Access denied")
            return False

        def _base_path(self) -> str:
            prefix = self.headers.get("X-Ingress-Path", "") if getattr(self, "_ingress", False) else ""
            return (prefix if INGRESS_PATH_RE.match(prefix) else "").rstrip("/") + "/"

        def _send_index(self, filename: str = "index.html") -> None:
            html = (STATIC / filename).read_text(encoding="utf-8")
            html = re.sub(r'\s*<base href="[^"]*">', "", html)
            html = html.replace("<head>", f'<head>\n  <base href="{self._base_path()}">\n'
                                          f'  <meta name="aula-version" content="{__version__}">', 1)
            self._send(html.encode(), "text/html; charset=utf-8")

        def _send(self, data: bytes, content_type: str, status: int = 200,
                  cache_control: str = "no-store", extra_headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", cache_control)
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def _send_static(self, data: bytes, content_type: str, extra_headers: dict[str, str] | None = None) -> None:
            """Shell assets: answer If-None-Match with 304 so reloads skip unchanged bytes."""
            etag = '"' + hashlib.sha256(data).hexdigest()[:32] + '"'
            headers = {**(extra_headers or {}), "ETag": etag}
            match = self.headers.get("If-None-Match", "")
            if match.strip() == "*" or etag in (tag.strip().removeprefix("W/") for tag in match.split(",")):
                self.send_response(304)
                self.send_header("Cache-Control", STATIC_CACHE)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                return
            self._send(data, content_type, cache_control=STATIC_CACHE, extra_headers=headers)

        def _send_subscription(self, head: bool = False) -> bool:
            """Serve the token-protected ICS without Access/ingress: calendar apps cannot log in."""
            path = self.path.split("?", 1)[0]
            if not path.startswith("/api/aula_calendar/"):
                return False
            self._ingress = self.client_address[0] == allowed_ip
            expected = controller.profiles.calendar_path
            if not CALENDAR_RE.match(path) or not secrets.compare_digest(path.encode(), expected.encode()):
                self.send_error(404)
                return True
            data = controller.calendar.content
            if head:
                self.send_response(200)
                self.send_header("Content-Type", "text/calendar; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
            else:
                self._send(data, "text/calendar; charset=utf-8",
                           extra_headers={"Content-Disposition": 'inline; filename="aula.ics"'})
            return True

        def do_HEAD(self) -> None:
            if not self._send_subscription(head=True):
                self.send_error(405)

        def _mcp_request(self, post: bool) -> bool:
            """Serve the secret-path MCP endpoint; every other path is left to the normal routes."""
            path = self.path.split("?", 1)[0]
            if not path.startswith("/mcp/"):
                return False
            self._ingress = self.client_address[0] == allowed_ip
            if not mcp.authorize(path):
                self.send_error(404)
                return True
            if not post:
                self.send_response(405)
                self.send_header("Allow", "POST")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return True
            if not mcp.admit():
                self._send(b'{"error":"Too many MCP requests. Wait a minute."}', "application/json", 429)
                return True
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MCP_MAX_BODY:
                    raise ValueError("Supply a JSON-RPC message")
                message = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeDecodeError):
                self._send(b'{"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"Parse error"}}',
                           "application/json", 400)
                return True
            reply = mcp.handle(message)
            if reply is None:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._send(json.dumps(reply, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return True

        def do_GET(self) -> None:
            if self._mcp_request(post=False):
                return
            if self._send_subscription():
                return
            if not self._authorized():
                return
            path = self.path.split("?", 1)[0]
            if path == "/api/version":
                self._send(json.dumps({"version": __version__}).encode(), "application/json; charset=utf-8")
                return
            if path == "/api/whoami":
                self._send(json.dumps({"user": getattr(self, "_identity", "")}).encode(),
                           "application/json; charset=utf-8")
                return
            if path == "/api/sickness":
                try:
                    result = sick_days.snapshot()
                except AulaApiError as error:
                    _LOGGER.warning("Sickness status unavailable: %s", describe(error))
                    self._send(json.dumps({"error": str(error)}).encode(), "application/json", 502)
                    return
                self._send(json.dumps(result).encode(), "application/json")
                return
            if path in ("/api/gallery/album", "/api/gallery/image"):
                query = parse_qs(self.path.partition("?")[2])
                album = query.get("album", query.get("id", [""]))[0]
                client, children = controller.source_client, controller.config.children
                try:
                    if not album.isdigit() or controller.thread_source is None:
                        raise ValueError("Invalid album")
                    if path == "/api/gallery/album":
                        self._send(json.dumps(gallery.album_detail(client, children, album)).encode(),
                                   "application/json")
                        return
                    name = ""
                    size = query.get("size", ["large"])[0]
                    cover = query.get("cover", [""])[0]
                    media_id = query.get("media", [""])[0]
                    if (cover and not cover.isdigit()) or (not cover and (not media_id.isdigit() or size not in gallery.SIZES)):
                        raise ValueError("Invalid photo")

                    def load_image():
                        nonlocal name
                        if cover:
                            url = gallery.cover_url(client, children, album, int(cover))
                        else:
                            url, name = gallery.media_url(client, children, album, media_id, size)
                        return download_attachment({"url": url})

                    if previews and query.get("download") != ["1"] and (cover or size in {"small", "medium", "thumb"}):
                        key = f"album:{album}:{media_id}:{size}:{cover}:{query.get('v', [''])[0]}"
                        data, content_type = previews.get_or_load(key, load_image)
                    else:
                        data, content_type = load_image()
                except (ValueError, LookupError, AulaApiError, requests.RequestException) as error:
                    _LOGGER.warning("event=gallery.failed error_type=%s", type(error).__name__)
                    self._send(b'{"error":"Photos could not be loaded. Open the album in Aula."}',
                               "application/json", 502 if not isinstance(error, LookupError) else 404)
                    return
                headers = {}
                if query.get("download") == ["1"]:
                    headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(name or 'photo.jpg', safe='')}"
                self._send(data, content_type, cache_control="private, max-age=1800", extra_headers=headers)
                return
            if path == "/api/feed":
                self._send(controller.feed_json, "application/json; charset=utf-8")
                return
            if path == "/api/feed/reading":
                query = parse_qs(self.path.partition("?")[2])
                item_id = query.get("id", [""])[0]
                item = _reading_item(controller, item_id)
                if item is None:
                    self._send(b'{"error":"Source item unavailable"}', "application/json", 404)
                    return
                # wait=1 holds the request until a running lookup finishes, so the page never polls.
                reading = (controller.related_reading.wait(item) if query.get("wait", [""])[0] == "1"
                           else controller.related_reading.snapshot(item))
                self._send(json.dumps(reading).encode(), "application/json")
                return
            if path == "/api/feed/attachment":
                query = parse_qs(self.path.partition("?")[2])
                item_id = query.get("id", [""])[0]
                item = next((entry for entry in controller.store.get("feed", [])
                             if entry.get("id") == item_id), None)
                if item is None:
                    self._send(b'{"error":"Source item unavailable"}', "application/json", 404)
                    return
                try:
                    index = int(query.get("index", [""])[0])
                    if index < 0:
                        raise ValueError("Invalid attachment index")
                    message_id = query.get("message", [""])[0]
                    if message_id:
                        if not item_id.startswith("thread:") or not any(
                                str(message.get("id")) == message_id for message in item.get("thread_messages", [])):
                            raise ValueError("Message unavailable")
                    preview = query.get("preview") == ["1"] and query.get("download") != ["1"]
                    expected_source = query.get("source", [""])[0]
                    if preview and not expected_source:
                        raise ValueError("Supply the preview source")
                    attachment = {}

                    def load_attachment():
                        nonlocal attachment
                        media = item.get("attachments", [])
                        if message_id:
                            messages = fetch_thread_messages(controller.source_client, item_id.removeprefix("thread:"))
                            message = next((row for row in messages if str(row.get("id")) == message_id), None)
                            if message is None:
                                raise ValueError("Message unavailable")
                            media = message.get("attachments", [])
                        elif item_id.startswith("post:"):
                            raw = _call(controller.source_client, "?" + urlencode({
                                "method": "posts.getById", "id": item_id.removeprefix("post:"),
                            })).get("data")
                            if not isinstance(raw, dict):
                                raise AulaApiError("Aula returned no post details")
                            content = raw.get("content") or {}
                            media = attachments(raw, content.get("html", "") if isinstance(content, dict) else content)
                        if index >= len(media):
                            raise ValueError("Attachment unavailable")
                        attachment = media[index]
                        if preview and (attachment.get("kind") != "image" or source_key(attachment.get("url", "")) != expected_source):
                            raise ValueError("Preview source changed; reopen the post")
                        return download_attachment(attachment)

                    if preview and previews:
                        key = f"post:{item_id}:{message_id}:{index}:{expected_source}"
                        data, content_type = previews.get_or_load(key, load_attachment)
                    else:
                        data, content_type = load_attachment()
                except (ValueError, AulaApiError, requests.RequestException) as error:
                    _LOGGER.warning("event=media.download_failed error_type=%s", type(error).__name__)
                    self._send(b'{"error":"Attachment could not be opened. It may be unavailable, protected, or larger than 25 MB. Open the source in Aula."}',
                               "application/json", 502)
                    return
                name = str(attachment.get("name") or "attachment")
                disposition = "attachment" if query.get("download") == ["1"] or content_type == "application/octet-stream" else "inline"
                self._send(data, content_type, cache_control="private, max-age=1800" if preview else "no-store", extra_headers={
                    "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(name, safe='')}"})
                return
            if path == "/api/feed/media":
                item_id = parse_qs(self.path.partition("?")[2]).get("id", [""])[0]
                item = next((entry for entry in controller.store.get("feed", [])
                             if entry.get("id") == item_id), None)
                if item is None:
                    self._send(b'{"error":"Source item unavailable"}', "application/json", 404)
                    return
                media = item.get("attachments", [])
                if item_id.startswith("post:"):
                    try:
                        raw = _call(controller.source_client, "?" + urlencode({
                            "method": "posts.getById", "id": item_id.removeprefix("post:"),
                        })).get("data")
                        if not isinstance(raw, dict):
                            raise AulaApiError("Aula returned no post details")
                        content = raw.get("content") or {}
                        body = content.get("html", "") if isinstance(content, dict) else content
                        media = attachments(raw, body)
                    except AulaApiError:
                        _LOGGER.warning("event=media.unavailable source_type=post")
                        self._send(json.dumps({
                            "attachments": media,
                            "error": "Aula attachments could not be checked. Open the original post in Aula to see any images.",
                        }).encode(), "application/json", 502)
                        return
                self._send(json.dumps({"attachments": media}).encode(), "application/json")
                return
            if path.startswith("/api/avatars/"):
                avatar = controller.avatars.get(path.removeprefix("/api/avatars/"))
                if avatar is None:
                    self.send_error(404)
                    return
                self._send(avatar[0], avatar[1])
                return
            if path == "/api/account":
                data = controller.account.snapshot() if controller.account else {
                    "mode": "integration", "connected": False,
                    "message": "Set data_source: direct in the add-on configuration to use its own Aula login.",
                    "children": controller.profiles.snapshot(),
                    "login": {"status": "idle", "message": "", "identities": []},
                }
                self._send(json.dumps(data).encode(), "application/json; charset=utf-8")
                return
            if path == "/api/profiles":
                mcp_key = getattr(controller.config, "mcp_key", "")
                mcp_path = f"/mcp/{mcp_key}" if mcp_key else ""
                public_url = getattr(controller.config, "public_url", "")
                self._send(json.dumps({
                    "children": controller.profiles.snapshot(),
                    "calendar_path": controller.profiles.calendar_path,
                    "calendar_url": (getattr(controller.config, "public_url", "") +
                                     controller.profiles.calendar_path
                                     if getattr(controller.config, "public_url", "") else ""),
                    "mcp_enabled": bool(mcp_key),
                    "mcp_path": mcp_path,
                    "mcp_url": public_url + mcp_path if public_url and mcp_path else "",
                    "mcp_allow_writes": bool(getattr(controller.config, "mcp_allow_writes", False)),
                    "calendar_count": controller.calendar.count,
                    "calendar_categories": controller.calendar.categories(),
                    "calendar_error": controller.calendar.error or controller.calendar_bridge_error,
                    "events": controller.calendar.snapshot_events(),
                    "archived_events": controller.calendar.snapshot_events(archived=True),
                    "recommendations": controller.calendar.recommendations(),
                    "declined_recommendations": controller.calendar.recommendations(archived=True),
                    "extraction_available": hasattr(controller.provider, "analyse"),
                    "recognition_warnings": [
                        {"id": item["id"], "title": item["title"], "warning": item["recognition_warning"]}
                        for item in controller.store.get("feed", [])
                        if item.get("recognition_warning")
                    ],
                }).encode(), "application/json; charset=utf-8")
                return
            if path == "/api/ai-settings":
                self._send(json.dumps(controller.ai_settings()).encode(), "application/json; charset=utf-8")
                return
            if path == "/api/sync-settings":
                self._send(json.dumps(controller.sync_settings.snapshot()).encode(),
                           "application/json; charset=utf-8")
                return
            if path in ("/api/notifications", "/api/notifications/history"):
                status, result = notifications.handle(controller, "GET", path)
                self._send(json.dumps(result).encode(), "application/json; charset=utf-8", status)
                return
            if path == "/calendar.ics":
                self._send(controller.calendar.content, "text/calendar; charset=utf-8")
                return
            if path == "/icon.svg":
                self._send_static((STATIC.parent.parent / "icon.svg").read_bytes(), "image/svg+xml")
                return
            if path == "/sw.js":
                self._send_static((STATIC / "sw.js").read_bytes(), "text/javascript; charset=utf-8",
                                  extra_headers={"Service-Worker-Allowed": "/"})
                return
            if path == "/manifest.webmanifest":
                self._send_static((STATIC / "manifest.webmanifest").read_bytes(),
                                  "application/manifest+json; charset=utf-8")
                return
            image = STATIC_FILE_RE.match(path)
            if image and path != "/icon.svg":
                target = STATIC / path.lstrip("/")
                if target.is_file() and target.resolve().parent in (STATIC.resolve(), (STATIC / "icons").resolve()):
                    self._send_static(target.read_bytes(), IMAGE_TYPES[path.rsplit(".", 1)[1]])
                    return
            if SPA_RE.match(path):
                self._send_index()
                return
            if path == "/":
                self._send_index()
                return
            assets = {
                "/": ("index.html", "text/html"),
                "/app.js": ("app.js", "text/javascript"),
                "/ai-settings.js": ("ai-settings.js", "text/javascript"),
                "/sync-settings.js": ("sync-settings.js", "text/javascript"),
                "/notifications.js": ("notifications.js", "text/javascript"),
                "/style.css": ("style.css", "text/css"),
                "/account": ("account.html", "text/html"),
                "/account.js": ("account.js", "text/javascript"),
                "/pwa.js": ("pwa.js", "text/javascript"),
                "/theme.js": ("theme.js", "text/javascript"),
                "/offline.html": ("offline.html", "text/html"),
            }
            if path not in assets:
                self.send_error(404)
                return
            filename, mime = assets[path]
            if mime == "text/html":
                self._send((STATIC / filename).read_bytes(), mime + "; charset=utf-8")
            else:
                self._send_static((STATIC / filename).read_bytes(), mime + "; charset=utf-8")

        def do_POST(self) -> None:
            if self._mcp_request(post=True):
                return
            if not self._authorized():
                return
            # A same-origin non-simple header prevents cross-origin requests
            # from initiating work through an authenticated ingress session.
            if self.headers.get("X-Aula-Request") not in ("refresh", "settings"):
                self.send_error(403)
                return
            if self.path == "/api/sickness":
                if self.headers.get("X-Aula-Request") != "settings":
                    self.send_error(403)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 512:
                        raise ValueError("Supply a child and today's date")
                    value = json.loads(self.rfile.read(length))
                    if (not isinstance(value, dict) or set(value) not in (
                            {"child", "date"}, {"child", "date", "sick"})
                            or not isinstance(value.get("sick", True), bool)):
                        raise ValueError("Supply a child, today's date and a sickness boolean")
                    result = sick_days.report(value["child"], value["date"], value.get("sick", True))
                except (ValueError, UnicodeDecodeError) as error:
                    self._send(json.dumps({"error": str(error)}).encode(), "application/json", 400)
                    return
                except PermissionError as error:
                    self._send(json.dumps({"error": str(error)}).encode(), "application/json", 403)
                    return
                except LookupError as error:
                    self._send(json.dumps({"error": str(error)}).encode(), "application/json", 404)
                    return
                except AulaApiError as error:
                    _LOGGER.warning("Sickness reporting failed: %s", describe(error))
                    self._send(json.dumps({"error": str(error)}).encode(), "application/json", 502)
                    return
                self._send(json.dumps(result).encode(), "application/json")
                return
            if self.path == "/api/gallery/tag":
                if self.headers.get("X-Aula-Request") != "settings":
                    self.send_error(403)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 1024:
                        raise ValueError("Supply an album, photo, child and tagged boolean")
                    value = json.loads(self.rfile.read(length))
                    if not isinstance(value, dict) or set(value) != {"album", "media", "child", "tagged"}:
                        raise ValueError("Supply an album, photo, child and tagged boolean")
                    result = gallery.set_child_tag(controller.source_client, controller.config.children,
                                                   value["album"], value["media"], value["child"], value["tagged"])
                except (ValueError, UnicodeDecodeError) as error:
                    self._send(json.dumps({"error": str(error)}).encode(), "application/json", 400)
                    return
                except PermissionError as error:
                    self._send(json.dumps({"error": str(error)}).encode(), "application/json", 403)
                    return
                except LookupError as error:
                    self._send(json.dumps({"error": str(error)}).encode(), "application/json", 404)
                    return
                except AulaApiError as error:
                    _LOGGER.warning("Photo tagging failed: %s", describe(error))
                    self._send(json.dumps({"error": str(error)}).encode(), "application/json", 502)
                    return
                self._send(json.dumps(result).encode(), "application/json")
                return
            if self.path == "/api/feed/reading":
                if self.headers.get("X-Aula-Request") != "settings":
                    self.send_error(403)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 2048:
                        raise ValueError("Supply a feed item id")
                    value = json.loads(self.rfile.read(length))
                    if (not isinstance(value, dict) or "id" not in value or not set(value) <= {"id", "regenerate"}
                            or not isinstance(value["id"], str)
                            or not isinstance(value.get("regenerate", False), bool)):
                        raise ValueError("Supply a feed item id")
                    item = _reading_item(controller, value["id"])
                    if item is None:
                        raise ValueError("Source item unavailable")
                except (ValueError, UnicodeDecodeError) as error:
                    self._send(json.dumps({"error": str(error)}).encode(), "application/json", 400)
                    return
                result = controller.related_reading.request(item, regenerate=value.get("regenerate", False))
                self._send(json.dumps(result).encode(), "application/json")
                return
            if self.path == "/api/feed/read":
                if self.headers.get("X-Aula-Request") != "settings":
                    self.send_error(403)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 512:
                        raise ValueError("Supply a conversation id")
                    value = json.loads(self.rfile.read(length))
                    if not isinstance(value, dict) or set(value) != {"id"} or not isinstance(value["id"], str):
                        raise ValueError("Supply a conversation id")
                    changed = controller.mark_read(value["id"])
                except (ValueError, UnicodeDecodeError) as error:
                    self._send(json.dumps({"error": str(error)}).encode(), "application/json", 400)
                    return
                except LookupError as error:
                    self._send(json.dumps({"error": str(error)}).encode(), "application/json", 404)
                    return
                except AulaApiError as error:
                    _LOGGER.warning("Marking a conversation read failed: %s", describe(error))
                    self._send(json.dumps({"error": str(error)}).encode(), "application/json", 502)
                    return
                self._send(json.dumps({"read": True, "changed": changed}).encode(), "application/json")
                return
            if self.path == "/api/feed/reprocess":
                if self.headers.get("X-Aula-Request") != "settings":
                    self.send_error(403)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 2048:
                        raise ValueError("Reprocess body must be 1-2048 bytes")
                    value = json.loads(self.rfile.read(length))
                    if not isinstance(value, dict) or set(value) != {"id"} or not isinstance(value["id"], str):
                        raise ValueError("Supply a feed item id")
                    controller.feed.request_reprocess(value["id"])
                    controller.request_refresh()
                except (ValueError, UnicodeDecodeError) as err:
                    self._send(json.dumps({"error": str(err)}).encode(), "application/json", 400)
                    return
                self._send(b'{"queued":true}', "application/json", 202)
                return
            if self.path in ("/api/auth/start", "/api/auth/cancel", "/api/auth/identity"):
                if self.headers.get("X-Aula-Request") != "settings":
                    self.send_error(403)
                    return
                if not controller.account:
                    self._send(b'{"error":"Select data_source: direct in the add-on options"}', "application/json", 400)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 2048:
                        raise ValueError("Login body must be 1-2048 bytes")
                    value = json.loads(self.rfile.read(length))
                    if not isinstance(value, dict):
                        raise ValueError("Supply a JSON object")
                    if self.path.endswith("/start"):
                        if set(value) != {"username"}:
                            raise ValueError("Supply only username")
                        controller.account.login.start(value["username"])
                    elif self.path.endswith("/identity"):
                        if set(value) != {"index"}:
                            raise ValueError("Supply only identity index")
                        controller.account.login.select_identity(value["index"])
                    else:
                        if value:
                            raise ValueError("Supply an empty object")
                        controller.account.login.cancel()
                except (ValueError, UnicodeDecodeError) as err:
                    self._send(json.dumps({"error": str(err)}).encode(), "application/json", 400)
                    return
                self._send(b'{"queued":true}', "application/json", 202)
                return
            if self.path in ("/api/notifications", "/api/notifications/test", "/api/notifications/read",
                             "/api/push/subscribe",
                             "/api/push/device", "/api/push/remove", "/api/push/test"):
                if self.headers.get("X-Aula-Request") != "settings":
                    self.send_error(403)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 8192:
                        raise ValueError("Notification body must be 1–8192 bytes")
                    value = json.loads(self.rfile.read(length))
                except (ValueError, UnicodeDecodeError) as err:
                    self._send(json.dumps({"error": str(err)}).encode(), "application/json", 400)
                    return
                try:
                    status, result = notifications.handle(controller, "POST", self.path, value)
                except Exception as err:  # noqa: BLE001 - report HA/storage failures to settings
                    _LOGGER.error("Notification settings request failed: %s", describe(err))
                    status, result = 500, {"error": "Could not save or send notifications; retry"}
                self._send(json.dumps(result).encode(), "application/json; charset=utf-8", status)
                return
            if self.path == "/api/sync-settings":
                if self.headers.get("X-Aula-Request") != "settings":
                    self.send_error(403)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 2048:
                        raise ValueError("Sync settings body must be 1-2048 bytes")
                    result = controller.set_sync_settings(json.loads(self.rfile.read(length)))
                except (ValueError, UnicodeDecodeError) as err:
                    self._send(json.dumps({"error": str(err)}).encode(), "application/json", 400)
                    return
                except OSError as err:
                    _LOGGER.error("Could not save sync settings: %s", describe(err))
                    self._send(b'{"error":"Could not persist sync settings"}', "application/json", 500)
                    return
                self._send(json.dumps({"saved": True, **result}).encode(), "application/json; charset=utf-8")
                return
            if self.path == "/api/ai-settings":
                if self.headers.get("X-Aula-Request") != "settings":
                    self.send_error(403)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 4096:
                        raise ValueError("AI settings body must be 1-4096 bytes")
                    value = json.loads(self.rfile.read(length))
                    if isinstance(value, dict) and set(value) == {"ai_providers"}:
                        controller.set_ai_providers(value["ai_providers"])
                    elif isinstance(value, dict) and set(value) == {"ai_credential"}:
                        controller.set_ai_credential(value["ai_credential"])
                    elif isinstance(value, dict) and set(value) == {"reading_entity"}:
                        controller.set_reading_entity(value["reading_entity"])
                    elif not isinstance(value, dict) or set(value) != {"ai_task_entities"}:
                        raise ValueError("Supply only ai_providers, ai_credential, ai_task_entities or reading_entity")
                    else:
                        controller.set_ai_task_entities(value["ai_task_entities"])
                except (ValueError, UnicodeDecodeError) as err:
                    self._send(json.dumps({"error": str(err)}).encode(), "application/json", 400)
                    return
                except LookupError as err:
                    self._send(json.dumps({"error": str(err)}).encode(), "application/json", 503)
                    return
                except OSError as err:
                    _LOGGER.error("Could not save AI settings: %s", err)
                    self._send(b'{"error":"Could not persist AI settings"}', "application/json", 500)
                    return
                self._send(json.dumps({"saved": True, **controller.ai_settings()}).encode(),
                           "application/json; charset=utf-8")
                return
            if self.path == "/api/calendar/review":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 8192:
                        raise ValueError("Review body must be 1–8192 bytes")
                    value = json.loads(self.rfile.read(length))
                    if not isinstance(value, dict) or not {"uid", "action"} <= set(value) or set(value) - {"uid", "action", "changes"}:
                        raise ValueError("Supply uid, action and optional changes")
                    if not isinstance(value["uid"], str) or not isinstance(value["action"], str):
                        raise ValueError("uid and action must be text")
                    controller.calendar.review(value["uid"], value["action"], value.get("changes"))
                    controller.publish_feed()
                except (ValueError, UnicodeDecodeError) as err:
                    self._send(json.dumps({"error": str(err)}).encode(), "application/json", 400)
                    return
                except OSError as err:
                    _LOGGER.error("Calendar review failed: %s", err)
                    self._send(b'{"error":"Could not persist or publish calendar review; retry"}', "application/json", 500)
                    return
                self._send(b'{"saved":true}', "application/json")
                return
            if self.path == "/api/calendar/archive":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 4096:
                        raise ValueError("Archive body must be 1–4096 bytes")
                    value = json.loads(self.rfile.read(length))
                    if not isinstance(value, dict) or set(value) != {"uid", "archived"}:
                        raise ValueError("Supply uid and archived")
                    if not isinstance(value["uid"], str) or not isinstance(value["archived"], bool):
                        raise ValueError("uid must be text and archived must be a boolean")
                    controller.calendar.set_archived(value["uid"], value["archived"])
                    controller.publish_feed()
                except (ValueError, UnicodeDecodeError) as err:
                    self._send(json.dumps({"error": str(err)}).encode(), "application/json", 400)
                    return
                except OSError as err:
                    _LOGGER.error("Calendar archive update failed: %s", err)
                    self._send(b'{"error":"Could not persist or publish calendar archive update; retry"}', "application/json", 500)
                    return
                self._send(b'{"saved":true}', "application/json")
                return
            if self.path == "/api/calendar/categories":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 4096:
                        raise ValueError("Category body must be 1–4096 bytes")
                    value = json.loads(self.rfile.read(length))
                    if not isinstance(value, dict) or set(value) != {"excluded"}:
                        raise ValueError("Supply only excluded")
                    controller.calendar.set_excluded_categories(value["excluded"])
                except (ValueError, UnicodeDecodeError) as err:
                    self._send(json.dumps({"error": str(err)}).encode(), "application/json", 400)
                    return
                except OSError as err:
                    _LOGGER.error("Calendar category update failed: %s", err)
                    self._send(b'{"error":"Could not save or publish calendar categories; retry"}', "application/json", 500)
                    return
                self._send(json.dumps({"saved": True, "calendar_count": controller.calendar.count,
                                       "calendar_categories": controller.calendar.categories()}).encode(),
                           "application/json")
                return
            if self.path == "/api/calendar/rotate":
                try:
                    controller.calendar.rotate()
                except OSError as err:
                    _LOGGER.error("Calendar link rotation failed: %s", err)
                    self._send(b'{"error":"Could not rotate calendar subscription"}', "application/json", 500)
                    return
                self._send(b'{"rotated":true}', "application/json")
                return
            if self.path.startswith("/api/profiles/"):
                slug = self.path.removeprefix("/api/profiles/")
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 4096:
                        raise ValueError("Settings body must be 1–4096 bytes")
                    value = json.loads(self.rfile.read(length))
                    controller.profiles.update(slug, value)
                except (ValueError, UnicodeDecodeError) as err:
                    self._send(json.dumps({"error": str(err)}).encode(), "application/json", 400)
                    return
                except OSError as err:
                    _LOGGER.error("Could not save profile settings: %s", err)
                    self._send(b'{"error":"Could not persist profile settings"}', "application/json", 500)
                    return
                controller.publish_feed()
                controller.request_refresh()
                self._send(b'{"saved":true}', "application/json")
                return
            if self.path != "/api/refresh":
                self.send_error(404)
                return
            controller.request_refresh()
            self._send(b'{"queued":true}', "application/json", 202)

        def log_message(self, fmt: str, *args: Any) -> None:
            # Base handler messages contain raw request lines and query strings.
            return

    class Server(ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            _LOGGER.exception("event=ingress.unhandled_error")

    server = Server(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": SERVE_POLL_INTERVAL},
                     name="ingress", daemon=True).start()
    _LOGGER.info("Ingress feed listening on port %d", server.server_port)
    return server
