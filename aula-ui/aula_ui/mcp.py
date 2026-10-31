"""Stateless, authenticated MCP Streamable HTTP endpoint for Aula UI."""

from __future__ import annotations

import base64
import json
import logging
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from . import gallery
from . import __version__
from .aula_api import AulaApiError
from .diagnostics import describe
from .media import download_attachment, feed_attachment

_LOGGER = logging.getLogger(__name__)
_MAX_REQUEST_BYTES = 1024 * 1024
_INLINE_IMAGE_BYTES = 4 * 1024 * 1024
MCP_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")


def _object(properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


TOOLS = [
    {
        "name": "aula_status",
        "description": "Add-on version, authenticated identity, discovered children, cached item count, last sync time and sync errors.",
        "inputSchema": _object({}),
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "list_feed",
        "description": "Search the add-on's cached Aula feed, newest first. Filter by child, content type, text, date range and limit.",
        "inputSchema": _object({
            "child": {"type": "string", "description": "Child name or slug"},
            "types": {"type": "array", "items": {"type": "string"}},
            "search": {"type": "string", "maxLength": 300},
            "since": {"type": "string", "description": "ISO date or datetime lower bound"},
            "until": {"type": "string", "description": "ISO date or datetime upper bound"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        }),
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "get_item",
        "description": "Full cached details for one feed item, including all messages in a thread, summaries, translations, and attachment indexes.",
        "inputSchema": _object({"id": {"type": "string"}}, ("id",)),
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "upcoming_events",
        "description": "Cached Aula calendar entries, weekplans and homework in the requested look-ahead window, soonest first.",
        "inputSchema": _object({
            "child": {"type": "string"},
            "days": {"type": "integer", "minimum": 1, "maximum": 90},
        }),
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "get_presence",
        "description": "Today's cached presence/check-in status per child, including expected times and naps where available.",
        "inputSchema": _object({"child": {"type": "string"}}),
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "get_attachment",
        "description": "Fetch one post or message attachment. Small images are returned inline; other files are included as base64 data.",
        "inputSchema": _object({
            "id": {"type": "string"},
            "index": {"type": "integer", "minimum": 0},
            "message": {"type": "string"},
        }, ("id", "index")),
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "get_album",
        "description": "List photo/video metadata and child tags in an Aula gallery album. Album ids come from feed items of type Album.",
        "inputSchema": _object({"album": {"type": "string"}}, ("album",)),
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "get_album_photo",
        "description": "Fetch one Aula gallery photo. Small images are returned inline; other files are included as base64 data.",
        "inputSchema": _object({
            "album": {"type": "string"},
            "media": {"type": "string"},
            "size": {"type": "string", "enum": ["small", "medium", "large"]},
        }, ("album", "media")),
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "sickness_status",
        "description": "Whether each child is currently reported sick today and whether Aula allows reporting.",
        "inputSchema": _object({}),
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "refresh",
        "description": "Queue a fresh Aula sync. Returns immediately; call aula_status afterwards. Avoid repeated calls.",
        "inputSchema": _object({}),
        "annotations": {"readOnlyHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "report_sickness",
        "description": "Report or withdraw sickness for a child today through Aula. Ask the user to confirm the exact child, action and date before calling; pass confirmed=true only after that confirmation. Aula permissions and current-date checks still apply.",
        "inputSchema": _object({
            "child": {"type": "string"},
            "date": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
            "sick": {"type": "boolean"},
            "confirmed": {"type": "boolean"},
        }, ("child", "date", "confirmed")),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
    },
]


class MCPProtocolError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


class AulaMCP:
    def __init__(self, controller, sick_days):
        self.controller = controller
        self.sick_days = sick_days

    def handle(self, request: object, identity: str) -> tuple[dict | None, int]:
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            raise MCPProtocolError(-32600, "Invalid JSON-RPC request")
        method = request.get("method")
        request_id = request.get("id")
        if not isinstance(method, str):
            raise MCPProtocolError(-32600, "A method is required")
        if "id" not in request:
            if method == "notifications/initialized":
                return None, 202
            raise MCPProtocolError(-32600, "Unsupported notification")
        if not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
            raise MCPProtocolError(-32600, "Invalid request id")
        params = request.get("params", {})
        if not isinstance(params, dict):
            return self._error(request_id, -32602, "Invalid params"), 200
        if method == "initialize":
            client_version = params.get("protocolVersion")
            protocol_version = client_version if client_version in MCP_PROTOCOL_VERSIONS else MCP_PROTOCOL_VERSIONS[0]
            return {
                "jsonrpc": "2.0", "id": request_id,
                "result": {
                    "protocolVersion": protocol_version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "aula-ui", "version": __version__},
                    "instructions": "All data comes from the authenticated Aula UI add-on and its existing Aula session. Ask before any sickness change.",
                },
            }, 200
        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}, 200
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}, 200
        if method == "tools/call":
            name, arguments = params.get("name"), params.get("arguments", {})
            if not isinstance(name, str) or not isinstance(arguments, dict):
                return self._error(request_id, -32602, "Tool name and object arguments are required"), 200
            try:
                result = self.call_tool(name, arguments, identity)
            except (ValueError, PermissionError, LookupError, AulaApiError) as error:
                _LOGGER.warning("event=mcp.tool_failed tool=%s error=%s", name, describe(error))
                result = {"content": [{"type": "text", "text": str(error)}], "isError": True}
            except Exception as error:  # noqa: BLE001 - prevent internal details reaching an agent
                _LOGGER.error("event=mcp.tool_failed tool=%s error_type=%s", name, type(error).__name__)
                result = {"content": [{"type": "text", "text": "The add-on could not complete this tool call."}],
                          "isError": True}
            return {"jsonrpc": "2.0", "id": request_id, "result": result}, 200
        if method.startswith("notifications/"):
            return self._error(request_id, -32600, "Notifications must not include an id"), 200
        return self._error(request_id, -32601, "Method not found"), 200

    @staticmethod
    def _error(request_id, code, message):
        return {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": code, "message": message}}

    def call_tool(self, name: str, arguments: dict, identity: str) -> dict:
        dispatch = {
            "aula_status": self._status,
            "list_feed": self._list_feed,
            "get_item": self._get_item,
            "upcoming_events": self._upcoming_events,
            "get_presence": self._get_presence,
            "get_attachment": self._get_attachment,
            "get_album": self._get_album,
            "get_album_photo": self._get_album_photo,
            "sickness_status": self._sickness_status,
            "refresh": self._refresh,
            "report_sickness": self._report_sickness,
        }
        handler = dispatch.get(name)
        if handler is None:
            raise ValueError(f"Unknown tool: {name}")
        return handler(arguments, identity)

    def _feed(self):
        try:
            feed = json.loads(self.controller.feed_json)
        except (ValueError, TypeError, AttributeError) as error:
            raise AulaApiError("The add-on feed cache is unavailable") from error
        if not isinstance(feed, dict) or not isinstance(feed.get("items"), list):
            raise AulaApiError("The add-on feed cache is unavailable")
        return feed

    def _status(self, args, identity):
        _check_args(args, set())
        feed = self._feed()
        return _text({
            "addon_version": __version__,
            "identity": identity,
            "updated_at": feed.get("updated_at"),
            "children": feed.get("children", []),
            "items_cached": len(feed["items"]),
            "errors": feed.get("errors", []),
            "ai_progress": feed.get("ai_progress"),
            "connection": "add-on",
        })

    def _list_feed(self, args, _identity):
        _check_args(args, {"child", "types", "search", "since", "until", "limit"})
        feed = self._feed()
        limit = args.get("limit", 20)
        if not _is_int(limit) or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer from 1 to 100")
        child = _child_slug(feed, args.get("child"))
        types = args.get("types")
        if types is not None and (not isinstance(types, list) or len(types) > 20
                                  or any(not isinstance(item, str) for item in types)):
            raise ValueError("types must be a list of content type names")
        search = args.get("search", "")
        if not isinstance(search, str) or len(search) > 300:
            raise ValueError("search must be at most 300 characters")
        start = _parse_bound(args.get("since"), self.controller.config.timezone, False)
        end = _parse_bound(args.get("until"), self.controller.config.timezone, True)
        wanted = {value.casefold() for value in types} if types else None
        rows = [
            item for item in feed["items"]
            if isinstance(item, dict)
            and (child is None or child in (item.get("child_slugs") or []))
            and (wanted is None or str(item.get("content_type", "")).casefold() in wanted)
            and _item_time(item, self.controller.config.timezone) >= start
            and _item_time(item, self.controller.config.timezone) <= end
            and _matches(item, search)
        ]
        rows.sort(key=lambda item: _item_time(item, self.controller.config.timezone), reverse=True)
        selected = rows[:limit]
        result = {"updated_at": feed.get("updated_at"), "count": len(selected),
                  "items": [_summarize(item) for item in selected]}
        if feed.get("errors"):
            result["errors"] = feed["errors"]
        return _text(result)

    def _get_item(self, args, _identity):
        _check_args(args, {"id"}, {"id"})
        item_id = args["id"]
        if not isinstance(item_id, str) or len(item_id) > 200:
            raise ValueError("id must be a feed item id")
        item = next((entry for entry in self._feed()["items"]
                     if isinstance(entry, dict) and entry.get("id") == item_id), None)
        if item is None:
            raise LookupError("Feed item not found in the add-on cache")
        result = {key: value for key, value in item.items()
                  if key not in ("attachments", "thread_messages")}
        result["thread_messages"] = [
            {**{key: value for key, value in message.items() if key != "attachments"},
             "attachments": [_attachment_meta(entry, index)
                             for index, entry in enumerate(message.get("attachments") or [])]}
            for message in item.get("thread_messages") or [] if isinstance(message, dict)
        ]
        result["attachments"] = [_attachment_meta(entry, index)
                                 for index, entry in enumerate(item.get("attachments") or [])]
        return _text(result)

    def _upcoming_events(self, args, _identity):
        _check_args(args, {"child", "days"})
        days = args.get("days", 14)
        if not _is_int(days) or not 1 <= days <= 90:
            raise ValueError("days must be an integer from 1 to 90")
        feed = self._feed()
        child = _child_slug(feed, args.get("child"))
        zone = ZoneInfo(self.controller.config.timezone)
        now = datetime.now(zone)
        start, end = now - timedelta(hours=12), now + timedelta(days=days)
        rows = [
            item for item in feed["items"]
            if isinstance(item, dict)
            and item.get("content_type") in {"Calendar", "Weekplan", "Homework"}
            and (child is None or child in (item.get("child_slugs") or []))
            and start <= _item_datetime(item, zone) <= end
        ]
        rows.sort(key=lambda item: _item_datetime(item, zone))
        return _text([_summarize(item) for item in rows])

    def _get_presence(self, args, _identity):
        _check_args(args, {"child"})
        feed = self._feed()
        child = _child_slug(feed, args.get("child"))
        return _text([row for row in feed.get("presence", [])
                      if isinstance(row, dict) and
                      (child is None or child in {row.get("slug"), row.get("child_slug"), row.get("child")})])

    def _get_attachment(self, args, _identity):
        _check_args(args, {"id", "index", "message"}, {"id", "index"})
        item_id, index = args["id"], args["index"]
        if not isinstance(item_id, str) or len(item_id) > 200 or not _is_int(index) or index < 0:
            raise ValueError("Supply a feed item id and non-negative attachment index")
        item = next((entry for entry in self._feed()["items"]
                     if isinstance(entry, dict) and entry.get("id") == item_id), None)
        if item is None:
            raise LookupError("Feed item not found in the add-on cache")
        message = args.get("message", "")
        if not isinstance(message, str) or len(message) > 200:
            raise ValueError("message must be a message id")
        attachment, data, content_type = feed_attachment(
            self.controller.source_client, item, item_id, index, message)
        return _file_result(data, content_type, attachment.get("name") or "attachment")

    def _get_album(self, args, _identity):
        _check_args(args, {"album"}, {"album"})
        album_id = _numeric_id(args["album"])
        if self.controller.thread_source is None:
            raise AulaApiError("Photo albums need the add-on's own Aula login")
        return _text(gallery.album_detail(self.controller.source_client, self.controller.config.children, album_id))

    def _get_album_photo(self, args, _identity):
        _check_args(args, {"album", "media", "size"}, {"album", "media"})
        album_id, media_id = _numeric_id(args["album"]), args["media"]
        size = args.get("size", "large")
        if not isinstance(media_id, str) or not media_id.isdigit() or size not in gallery.SIZES:
            raise ValueError("Supply valid album, media and size values")
        if self.controller.thread_source is None:
            raise AulaApiError("Photo albums need the add-on's own Aula login")
        url, name = gallery.media_url(self.controller.source_client, self.controller.config.children,
                                      album_id, media_id, size)
        data, content_type = download_attachment({"url": url})
        return _file_result(data, content_type, name or f"album-{album_id}-{media_id}")

    def _sickness_status(self, args, _identity):
        _check_args(args, set())
        return _text(self.sick_days.snapshot())

    def _refresh(self, args, _identity):
        _check_args(args, set())
        self.controller.request_refresh()
        return _text({"queued": True})

    def _report_sickness(self, args, _identity):
        _check_args(args, {"child", "date", "sick", "confirmed"}, {"child", "date", "confirmed"})
        if args["confirmed"] is not True:
            raise ValueError("Ask the user to confirm the child, action and date before reporting sickness")
        child = _child_slug({"children": self.controller.profiles.snapshot()}, args["child"])
        day, sick = args["date"], args.get("sick", True)
        if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise ValueError("date must be a valid YYYY-MM-DD date")
        try:
            date.fromisoformat(day)
        except ValueError as error:
            raise ValueError("date must be a valid YYYY-MM-DD date") from error
        if not isinstance(sick, bool):
            raise ValueError("sick must be a boolean")
        status = self.sick_days.snapshot()
        child_status = next((entry for entry in status.get("children", [])
                             if entry.get("slug") == child), None)
        if child_status is None:
            raise LookupError("Aula returned no sickness status for this child")
        if not child_status.get("can_report"):
            raise PermissionError(child_status.get("reason") or "Aula does not allow sickness reporting for this child")
        return _text(self.sick_days.report(child, day, sick))


def _check_args(value, allowed, required=()):
    if set(value) - allowed or set(required) - set(value):
        raise ValueError("Arguments do not match this tool's input schema")


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _text(value):
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, default=str)}]}


def _file_result(data, content_type, name):
    summary = {"name": _safe_name(name), "content_type": content_type, "bytes": len(data)}
    content = [{"type": "text", "text": json.dumps(summary, ensure_ascii=False)}]
    if content_type.startswith("image/") and len(data) <= _INLINE_IMAGE_BYTES:
        content.append({"type": "image", "data": base64.b64encode(data).decode("ascii"),
                        "mimeType": content_type.split(";", 1)[0]})
    else:
        summary["base64"] = base64.b64encode(data).decode("ascii")
        content[0]["text"] = json.dumps(summary, ensure_ascii=False)
    return {"content": content}


def _safe_name(value):
    return re.sub(r"[\x00-\x1f/\\]", "_", str(value or "file")).lstrip(".")[:150] or "file"


def _attachment_meta(value, index):
    value = value if isinstance(value, dict) else {}
    return {"index": index, "name": value.get("name") or "Attachment",
            "type": value.get("kind") or value.get("type") or "unknown"}


def _child_slug(feed, child):
    if child is None or child == "":
        return None
    if not isinstance(child, str):
        raise ValueError("child must be a name or slug")
    key = child.strip().casefold()
    children = feed.get("children", [])
    matches = [entry for entry in children if isinstance(entry, dict)
               and (str(entry.get("slug", "")).casefold() == key
                    or str(entry.get("name", "")).casefold() == key
                    or str(entry.get("display_name", "")).casefold() == key)]
    if len(matches) != 1:
        raise ValueError("Unknown or ambiguous child. Use a configured child name or slug.")
    return matches[0]["slug"]


def _numeric_id(value):
    if not isinstance(value, str):
        raise ValueError("album must be a numeric id or album:<id>")
    match = re.fullmatch(r"(?:album:)?(\d+)", value.strip())
    if not match:
        raise ValueError("album must be a numeric id or album:<id>")
    return match.group(1)


def _parse_bound(value, timezone, upper):
    if value is None:
        return datetime.min.replace(tzinfo=ZoneInfo(timezone)) if not upper else datetime.max.replace(
            tzinfo=ZoneInfo(timezone))
    if not isinstance(value, str):
        raise ValueError("since and until must be ISO dates or datetimes")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            day = date.fromisoformat(value)
            zone = ZoneInfo(timezone)
            return datetime.combine(day, time.max if upper else time.min, tzinfo=zone)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("since and until must be valid ISO dates or datetimes") from error
    return parsed.replace(tzinfo=ZoneInfo(timezone)) if parsed.tzinfo is None else parsed


def _item_datetime(item, zone):
    value = str(item.get("date") or "")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=zone)
    return parsed.replace(tzinfo=zone) if parsed.tzinfo is None else parsed


def _item_time(item, timezone):
    return _item_datetime(item, ZoneInfo(timezone))


def _matches(item, needle):
    if not needle:
        return True
    parts = [item.get("title"), item.get("translated_title"), item.get("summary"),
             item.get("original_text"), item.get("sender")]
    parts.extend(
        " ".join(str(message.get(field) or "") for field in ("sender", "original_text", "text"))
        for message in item.get("thread_messages") or [] if isinstance(message, dict)
    )
    return any(needle.casefold() in value.casefold() for value in parts if isinstance(value, str))


def _summarize(item):
    result = {
        "id": item.get("id"),
        "type": item.get("content_type"),
        "date": item.get("date"),
        "title": item.get("translated_title") or item.get("title"),
        "sender": item.get("sender"),
        "children": item.get("children"),
        "summary": item.get("summary"),
        "preview": str(item.get("original_text") or "")[:300] if not item.get("summary") else None,
        "messages": len(item.get("thread_messages") or []) or None,
        "comments": item.get("comment_count"),
        "attachments": len(item.get("attachments") or []) or None,
        "unread": item.get("unread", item.get("is_unread")),
    }
    return {key: value for key, value in result.items()
            if value is not None and value != "" and value != []}
