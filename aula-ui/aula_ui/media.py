"""Preserve Aula attachment metadata without interpreting embedded HTML."""

from html.parser import HTMLParser
from urllib.parse import urlencode, urlsplit
import requests

from .avatars import sniff

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
DOWNLOAD_HOSTS = {"media-prod.aula.dk", "media.aula.dk", "files.aula.dk"}


def download_attachment(attachment):
    """Fetch only an existing Aula media reference, without forwarding credentials."""
    url = image_url(attachment.get("url"))
    parsed = urlsplit(url)
    if parsed.hostname not in DOWNLOAD_HOSTS or parsed.port not in (None, 443):
        raise ValueError("This attachment cannot be downloaded through the app")
    with requests.get(url, timeout=(10, 30), stream=True, allow_redirects=False) as response:
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError("Aula attachment is unavailable")
        body = bytearray()
        for chunk in response.iter_content(65536):
            body.extend(chunk)
            if len(body) > MAX_ATTACHMENT_BYTES:
                raise ValueError("Attachment exceeds the 25 MB in-app limit")
        data = bytes(body)
        declared = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        content_type = "application/pdf" if data.startswith(b"%PDF-") else sniff(data[:16])
        if not content_type and declared in {"video/mp4", "audio/mpeg", "audio/mp4", "audio/ogg", "video/webm"}:
            content_type = declared
        return data, content_type or "application/octet-stream"


def image_url(value):
    if not isinstance(value, str):
        return ""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    return value if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password else ""


class InlineImages(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.images = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "img":
            values = dict(attrs)
            self.images.append({
                "name": values.get("alt") or "Attached image",
                "url": image_url(values.get("src")), "kind": "image",
            })


def attachments(raw, body=""):
    parser = InlineImages()
    parser.feed(str(body or ""))
    result = list(parser.images)
    rows = raw.get("attachments") or []
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                result.append({"name": "Attachment", "kind": "file", "url": ""})
                continue
            media = row.get("media") if isinstance(row.get("media"), dict) else {}
            file = media.get("file") or row.get("file")
            file = file if isinstance(file, dict) else {}
            if isinstance(file.get("file"), dict):
                file = file["file"]
            name = row.get("name") or file.get("name") or media.get("title") or "Attachment"
            url = file.get("url") or media.get("url") or row.get("url") or media.get("thumbnailUrl") or ""
            is_image = str(media.get("mediaType", "")).lower() in ("image", "photo", "picture")
            is_image = is_image or str(name).lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp"))
            kind = "image" if is_image else "file"
            media_type = str(media.get("mediaType", "")).lower()
            if media_type in ("video", "audio"):
                kind = media_type
            elif str(name).lower().endswith((".mp4", ".webm", ".mov")):
                kind = "video"
            elif str(name).lower().endswith((".mp3", ".m4a", ".ogg", ".wav")):
                kind = "audio"
            entry = {"name": str(name), "kind": kind, "url": image_url(url)}
            status = str(file.get("scanningStatus") or "").lower()
            if status in ("blocked", "processing"):
                entry.update(url="", unavailable_reason="Blocked by Aula" if status == "blocked" else "Aula is scanning this file")
            if row.get("document") and not entry["url"]:
                entry["unavailable_reason"] = "Protected Aula document; open in Aula to authenticate"
            result.append(entry)
    if not result and (raw.get("hasAttachments") or raw.get("attachmentCount")):
        result.append({"name": "Attachments reported by Aula", "kind": "unknown", "url": ""})
    return result


def feed_attachment(client, item, item_id, index, message_id="", fetch_messages=None,
                    call=None, download=None):
    """Resolve and download an attachment from one cached feed item."""
    from .aula_api import AulaApiError, _call, fetch_thread_messages

    fetch_messages = fetch_messages or fetch_thread_messages
    call = call or _call
    download = download or download_attachment
    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
        raise ValueError("Invalid attachment index")
    media = item.get("attachments", [])
    if message_id:
        if not item_id.startswith("thread:") or not any(
                str(message.get("id")) == message_id for message in item.get("thread_messages", [])):
            raise ValueError("Message unavailable")
        messages = fetch_messages(client, item_id.removeprefix("thread:"))
        message = next((row for row in messages if str(row.get("id")) == message_id), None)
        if message is None:
            raise ValueError("Message unavailable")
        media = message.get("attachments", [])
    elif item_id.startswith("post:"):
        raw = call(client, "?" + urlencode({
            "method": "posts.getById", "id": item_id.removeprefix("post:"),
        })).get("data")
        if not isinstance(raw, dict):
            raise AulaApiError("Aula returned no post details")
        content = raw.get("content") or {}
        media = attachments(raw, content.get("html", "") if isinstance(content, dict) else content)
    if not isinstance(media, list) or index >= len(media):
        raise ValueError("Attachment unavailable")
    attachment = media[index]
    if not isinstance(attachment, dict):
        raise ValueError("Attachment unavailable")
    data, content_type = download(attachment)
    return attachment, data, content_type
