"""Model Context Protocol endpoint (stateless streamable HTTP, JSON responses).

Authorised by a secret in the URL path because MCP connectors such as claude.ai can only
carry a URL. The tools are a narrow, mostly read-only view of data the add-on already has;
they never accept arbitrary Aula API methods.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import secrets
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

from . import __version__, gallery
from .aula_api import AulaApiError, _call, fetch_thread_messages
from .media import attachments, download_attachment
from .messages import date_key

_LOGGER = logging.getLogger(__name__)

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MCP_PATH_RE = re.compile(r"^/mcp/([A-Za-z0-9_-]{32,128})$")
MAX_BODY = 64 * 1024
MAX_FILE_BYTES = 5 * 1024 * 1024
EVENT_TYPES = {"calendar", "weekplan", "homework"}
RATE_LIMIT_PER_MINUTE = 60


class Blocks(list):
    """Ready-made MCP content blocks, as opposed to data to be serialised as JSON text."""


class ToolError(Exception):
    """A failure reported to the model as a tool result, not a protocol error."""


def _text(value: Any) -> dict[str, Any]:
    return {"type": "text", "text": value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)}


def _timestamp(item: dict[str, Any]) -> float:
    return date_key(item)


def _parse_bound(value: Any, name: str) -> float:
    if value in (None, ""):
        return 0.0
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as err:
        raise ToolError(f"{name} must be an ISO date or date-time") from err
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()


def summarize(item: dict[str, Any]) -> dict[str, Any]:
    summary = item.get("summary")
    original = item.get("original_text")
    out = {
        "id": item.get("id"), "type": item.get("content_type"), "date": item.get("date"),
        "title": item.get("translated_title") or item.get("title"),
        "sender": item.get("sender"), "children": item.get("children"), "summary": summary,
        "preview": str(original)[:300] if not summary and original else None,
        "messages": len(item.get("thread_messages") or []) or None,
        "comments": item.get("comment_count") or None,
        "attachments": len(item.get("attachments") or []) or None,
    }
    return {key: value for key, value in out.items() if value not in (None, "", [])}


class McpEndpoint:
    def __init__(self, controller: Any, sick_days: Any, key: str, allow_writes: bool = False) -> None:
        self.controller = controller
        self.sick_days = sick_days
        self.key = key
        self.allow_writes = allow_writes
        self._lock = threading.Lock()
        self._requests: deque[float] = deque()
        self.tools = self._tool_table()

    # -- auth / throttling -------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    def authorize(self, path: str) -> bool:
        match = MCP_PATH_RE.match(path)
        return bool(self.enabled and match and secrets.compare_digest(match.group(1).encode(), self.key.encode()))

    def admit(self) -> bool:
        with self._lock:
            now = time.monotonic()
            while self._requests and now - self._requests[0] >= 60:
                self._requests.popleft()
            if len(self._requests) >= RATE_LIMIT_PER_MINUTE:
                return False
            self._requests.append(now)
            return True

    # -- JSON-RPC ----------------------------------------------------------

    def handle(self, message: Any) -> dict[str, Any] | list[dict[str, Any]] | None:
        """Return the JSON-RPC response, or None when only notifications were sent."""
        if isinstance(message, list):
            replies = [reply for reply in (self._one(entry) for entry in message) if reply is not None]
            return replies or None
        return self._one(message)

    def _one(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
        request_id = message.get("id")
        if "id" not in message:
            return None
        method, params = message["method"], message.get("params") or {}
        try:
            if method == "initialize":
                wanted = params.get("protocolVersion")
                result = {
                    "protocolVersion": wanted if wanted in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "aula-ui", "version": __version__},
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [spec for spec, _ in self.tools.values()]}
            elif method == "tools/call":
                result = self._call(params)
            else:
                return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}
        except ValueError as err:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": str(err)}}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _call(self, params: dict[str, Any]) -> dict[str, Any]:
        name, args = params.get("name"), params.get("arguments", {})
        args = {} if args is None else args
        if name not in self.tools:
            raise ValueError(f"Unknown tool: {name}")
        if not isinstance(args, dict):
            raise ValueError("arguments must be an object")
        try:
            content = self.tools[name][1](args)
            return {"content": list(content) if isinstance(content, Blocks) else [_text(content)]}
        except ToolError as err:
            return {"content": [_text(str(err))], "isError": True}
        except (AulaApiError, LookupError, PermissionError, ValueError) as err:
            return {"content": [_text(f"Error: {err}")], "isError": True}

    # -- tools ---------------------------------------------------------------

    def _feed(self) -> dict[str, Any]:
        return json.loads(self.controller.feed_json)

    def _slug(self, feed: dict[str, Any], child: Any) -> str | None:
        if not child:
            return None
        key = str(child).strip().casefold()
        hits = [c for c in feed.get("children", []) if key in (str(c.get("slug", "")).casefold(), str(c.get("name", "")).casefold())]
        if len(hits) != 1:
            names = ", ".join(f"{c.get('name')} ({c.get('slug')})" for c in feed.get("children", []))
            raise ToolError(f'Unknown child "{child}". Known children: {names or "none"}.')
        return hits[0]["slug"]

    def _select(self, feed: dict[str, Any], args: dict[str, Any], types: set[str] | None = None) -> list[dict[str, Any]]:
        slug = self._slug(feed, args.get("child"))
        wanted = types or ({str(t).casefold() for t in args["types"]} if args.get("types") else None)
        start, end = _parse_bound(args.get("since"), "since"), _parse_bound(args.get("until"), "until") or float("inf")
        needle = str(args.get("search") or "").casefold()
        rows = []
        for item in feed.get("items", []):
            if item.get("content_type") == "Presence":
                continue
            if slug and slug not in (item.get("child_slugs") or []):
                continue
            if wanted and str(item.get("content_type")).casefold() not in wanted:
                continue
            stamp = _timestamp(item)
            if (start or end != float("inf")) and not start <= stamp <= end:
                continue
            if needle and not any(needle in str(part).casefold() for part in (
                    item.get("title"), item.get("translated_title"), item.get("summary"), item.get("original_text"),
                    item.get("sender"), *[m.get("original_text") for m in item.get("thread_messages") or []])):
                continue
            rows.append(item)
        return rows

    def _status(self, _args: dict[str, Any]) -> Any:
        feed = self._feed()
        return {
            "addon_version": __version__, "updated_at": feed.get("updated_at"),
            "children": [{"name": c.get("name"), "slug": c.get("slug"), "institution_type": c.get("institution_type")}
                         for c in feed.get("children", [])],
            "items_cached": len(feed.get("items", [])), "errors": feed.get("errors", []),
            "ai_progress": feed.get("ai_progress"), "writes_enabled": self.allow_writes,
        }

    def _list_feed(self, args: dict[str, Any]) -> Any:
        feed = self._feed()
        limit = args.get("limit", 20)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ToolError("limit must be between 1 and 100")
        rows = sorted(self._select(feed, args), key=_timestamp, reverse=True)[:limit]
        return {"updated_at": feed.get("updated_at"), "count": len(rows), "items": [summarize(i) for i in rows]}

    def _get_item(self, args: dict[str, Any]) -> Any:
        item = next((i for i in self._feed().get("items", []) if i.get("id") == args.get("id")), None)
        if item is None:
            raise ToolError(f"Item {args.get('id')} not found.")
        out = dict(item)
        out["attachments"] = [{"index": n, "name": a.get("name"), "type": a.get("type")}
                              for n, a in enumerate(item.get("attachments") or [])]
        out["thread_messages"] = [
            {**m, "attachments": [{"index": n, "name": a.get("name"), "type": a.get("type")}
                                  for n, a in enumerate(m.get("attachments") or [])] or None}
            for m in item.get("thread_messages") or []]
        return out

    def _upcoming(self, args: dict[str, Any]) -> Any:
        days = args.get("days", 14)
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 90:
            raise ToolError("days must be between 1 and 90")
        feed = self._feed()
        now = datetime.now(timezone.utc)
        low, high = (now - timedelta(hours=12)).timestamp(), (now + timedelta(days=days)).timestamp()
        rows = [i for i in self._select(feed, args, EVENT_TYPES) if low <= _timestamp(i) <= high]
        return [summarize(i) for i in sorted(rows, key=_timestamp)]

    def _presence(self, args: dict[str, Any]) -> Any:
        feed = self._feed()
        slug = self._slug(feed, args.get("child"))
        return [p for p in feed.get("presence", []) if not slug or slug in (p.get("slug"), p.get("child_slug"), p.get("child"))]

    def _file(self, data: bytes, content_type: str, name: str) -> Blocks:
        if len(data) > MAX_FILE_BYTES:
            raise ToolError(f"{name} is {len(data) // 1024} KB, larger than the {MAX_FILE_BYTES // 1024 // 1024} MB limit. Open it in Aula.")
        mime = content_type.split(";")[0] or "application/octet-stream"
        encoded = base64.b64encode(data).decode()
        meta = _text({"name": name, "content_type": mime, "bytes": len(data)})
        if mime.startswith("image/"):
            return Blocks([meta, {"type": "image", "data": encoded, "mimeType": mime}])
        return Blocks([meta, {"type": "resource", "resource": {"uri": f"aula://file/{name}", "mimeType": mime, "blob": encoded}}])

    def _attachment(self, args: dict[str, Any]) -> Any:
        item_id, index, message_id = args.get("id"), args.get("index"), args.get("message")
        if not isinstance(item_id, str) or isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ToolError("Supply the item id and a non-negative attachment index")
        item = next((e for e in self.controller.store.get("feed", []) if e.get("id") == item_id), None)
        if item is None:
            raise ToolError("Source item unavailable")
        client, media = self.controller.source_client, item.get("attachments", [])
        if message_id:
            if not item_id.startswith("thread:") or not any(str(m.get("id")) == str(message_id) for m in item.get("thread_messages", [])):
                raise ToolError("Message unavailable")
            rows = fetch_thread_messages(client, item_id.removeprefix("thread:"))
            row = next((r for r in rows if str(r.get("id")) == str(message_id)), None)
            if row is None:
                raise ToolError("Message unavailable")
            media = row.get("attachments", [])
        elif item_id.startswith("post:"):
            raw = _call(client, "?" + urlencode({"method": "posts.getById", "id": item_id.removeprefix("post:")})).get("data")
            if not isinstance(raw, dict):
                raise ToolError("Aula returned no post details")
            content = raw.get("content") or {}
            media = attachments(raw, content.get("html", "") if isinstance(content, dict) else content)
        if index >= len(media):
            raise ToolError("Attachment unavailable")
        data, content_type = download_attachment(media[index])
        return self._file(data, content_type, str(media[index].get("name") or "attachment"))

    @staticmethod
    def _album_id(value: Any) -> str:
        match = re.search(r"(\d+)\s*$", str(value or ""))
        if not match:
            raise ToolError(f"Invalid album id: {value}")
        return match.group(1)

    def _album(self, args: dict[str, Any]) -> Any:
        return gallery.album_detail(self.controller.source_client, self.controller.config.children, self._album_id(args.get("album")))

    def _album_photo(self, args: dict[str, Any]) -> Any:
        album, media, size = self._album_id(args.get("album")), str(args.get("media") or ""), args.get("size", "large")
        url, name = gallery.media_url(self.controller.source_client, self.controller.config.children, album, media, size)
        data, content_type = download_attachment({"url": url})
        return self._file(data, content_type, name or f"{album}-{media}.jpg")

    def _sickness(self, _args: dict[str, Any]) -> Any:
        return self.sick_days.snapshot()

    def _refresh(self, _args: dict[str, Any]) -> Any:
        self.controller.request_refresh()
        return {"queued": True}

    def _report_sickness(self, args: dict[str, Any]) -> Any:
        child, day, sick = args.get("child"), args.get("date"), args.get("sick", True)
        feed = self._feed()
        return self.sick_days.report(self._slug(feed, child), day, sick)

    def _tool_table(self) -> dict[str, tuple[dict[str, Any], Any]]:
        child = {"type": "string", "description": "Child name or slug"}
        read = {"readOnlyHint": True, "openWorldHint": False}

        def tool(name: str, description: str, properties: dict[str, Any], handler: Any,
                 required: tuple[str, ...] = (), annotations: dict[str, Any] | None = None):
            schema = {"type": "object", "properties": properties, "additionalProperties": False}
            if required:
                schema["required"] = list(required)
            return name, ({"name": name, "description": description, "inputSchema": schema,
                           "annotations": annotations or read}, handler)

        tools = [
            tool("aula_status", "Add-on version, children, last sync time and sync errors.", {}, self._status),
            tool("list_feed", "Newest-first Aula feed (posts, message threads, calendar, homework, weekplans, albums) with AI "
                 "summaries. Use get_item for full text.", {
                     "child": child, "types": {"type": "array", "items": {"type": "string"}},
                     "search": {"type": "string"}, "since": {"type": "string", "description": "ISO date/time"},
                     "until": {"type": "string", "description": "ISO date/time"},
                     "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, self._list_feed),
            tool("get_item", "Full details of one feed item: complete text, summary/translation, every thread message and "
                 "attachment indexes.", {"id": {"type": "string"}}, self._get_item, ("id",)),
            tool("upcoming_events", "Calendar events, weekplan entries and homework for the coming days, soonest first.", {
                "child": child, "days": {"type": "integer", "minimum": 1, "maximum": 90}}, self._upcoming),
            tool("get_presence", "Today's presence/check-in status per child.", {"child": child}, self._presence),
            tool("get_attachment", "Fetch an attachment of a post or message (returned inline, max 5 MB).", {
                "id": {"type": "string"}, "index": {"type": "integer", "minimum": 0},
                "message": {"type": "string", "description": "Message id inside a thread"}}, self._attachment, ("id", "index")),
            tool("get_album", "List photos/videos in a gallery album.", {"album": {"type": "string"}}, self._album, ("album",)),
            tool("get_album_photo", "Fetch one gallery photo (returned inline, max 5 MB).", {
                "album": {"type": "string"}, "media": {"type": "string"},
                "size": {"type": "string", "enum": ["small", "medium", "large"]}}, self._album_photo, ("album", "media")),
            tool("sickness_status", "Whether each child is reported sick today and whether you may report it.", {}, self._sickness),
            tool("refresh", "Ask the add-on to sync with Aula now. Returns immediately.", {}, self._refresh,
                 annotations={"readOnlyHint": False, "idempotentHint": True, "openWorldHint": False}),
        ]
        if self.allow_writes:
            tools.append(tool(
                "report_sickness", "Report or withdraw a child's sickness for today in Aula, notifying the daycare/SFO. "
                "Confirm with the user first.", {
                    "child": child, "date": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$", "description": "Today, YYYY-MM-DD"},
                    "sick": {"type": "boolean"}}, self._report_sickness, ("child", "date"),
                {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True}))
        return dict(tools)
