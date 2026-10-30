"""Download and cache children's Aula profile pictures for same-origin display.

Aula returns short-lived signed picture URLs, so the image bytes are cached in
the add-on data directory and served through ingress instead of hot-linking.
"""

import hashlib
import json
import logging
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

from . import __version__
from .auth_storage import atomic_write

_LOGGER = logging.getLogger(__name__)
MAX_BYTES = 3 * 1024 * 1024
REFRESH_SECONDS = 7 * 24 * 3600
RETRY_SECONDS = 6 * 3600
SLUG_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
# Object stores often label pictures generically; the bytes decide the type.
GENERIC_TYPES = {"", "application/octet-stream", "binary/octet-stream"}


def sniff(data):
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def picture_url(value):
    """Return an https picture URL from an Aula profile/profilePicture shape."""
    if isinstance(value, dict):
        if "profilePicture" in value:
            return picture_url(value["profilePicture"])
        if "institutionProfile" in value:
            return picture_url(value["institutionProfile"])
        value = value.get("url")
    if not isinstance(value, str):
        return ""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return ""
    return value


AUTHOR_LIMIT = 1000
_pending_authors = {}
_pending_lock = threading.Lock()


def author_key(name):
    return "p-" + hashlib.sha256(name.strip().casefold().encode()).hexdigest()[:16]


def note_author(name, profile):
    """Remember an author's picture seen while parsing Aula responses.

    Fetch helpers have no controller, so pictures are collected here and
    downloaded by ``Avatars.refresh_authors`` after the sync completes.
    """
    name = str(name or "").strip()
    url = picture_url(profile) if isinstance(profile, dict) else ""
    if not name or not url:
        return
    with _pending_lock:
        if name in _pending_authors or len(_pending_authors) < AUTHOR_LIMIT:
            _pending_authors[name] = url


def _source_key(url):
    # Signed query strings change on every login; the object path identifies the picture.
    parsed = urlsplit(url)
    return hashlib.sha256(f"{parsed.hostname}{parsed.path}".encode()).hexdigest()[:16]


class Avatars:
    def __init__(self, data_dir, session=None):
        self.directory = Path(data_dir) / "avatars"
        self.lock = threading.Lock()
        self.session = session or requests.Session()
        self._authors = None

    def _index(self):
        try:
            value = json.loads((self.directory / "authors.json").read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _due(self, slug, url, now):
        meta = self._meta(slug)
        key = _source_key(url)
        if self.version(slug) and meta.get("source") == key and now - meta.get("fetched_at", 0) < REFRESH_SECONDS:
            return False
        return not (meta.get("failed_source") == key and meta.get("failed_version") == __version__
                    and now - meta.get("failed_at", 0) < RETRY_SECONDS)

    def refresh_authors(self, limit=25, now=None):
        """Download up to ``limit`` new or stale author pictures; others wait for the next sync."""
        now = time.time() if now is None else now
        with _pending_lock:
            pending = dict(_pending_authors)
            _pending_authors.clear()
        if not pending:
            return 0
        index = self._index()
        before = dict(index)
        downloaded = 0
        for name, url in pending.items():
            key = author_key(name)
            if key not in index.values() and len(index) >= AUTHOR_LIMIT:
                continue
            index[name] = key
            if downloaded < limit and self._due(key, url, now):
                downloaded += 1
                self.refresh(key, [url], now)
        self._authors = None
        if index != before:
            self.directory.mkdir(parents=True, exist_ok=True)
            atomic_write(self.directory / "authors.json", json.dumps(index, ensure_ascii=False).encode())
        if downloaded:
            _LOGGER.info("event=avatar.authors seen=%d attempted=%d", len(pending), downloaded)
        return downloaded

    def authors(self):
        """Map author display names to their cache key and version for the frontend."""
        if self._authors is not None:
            return self._authors
        result = {}
        for name, key in self._index().items():
            version = self.version(key) if isinstance(key, str) else ""
            if version:
                result[name] = {"key": key, "v": version}
        self._authors = result
        return result

    def _paths(self, slug):
        return self.directory / f"{slug}.img", self.directory / f"{slug}.json"

    def _meta(self, slug):
        _, meta = self._paths(slug)
        try:
            return json.loads(meta.read_text())
        except (OSError, ValueError):
            return {}

    def version(self, slug):
        if not SLUG_RE.match(slug or ""):
            return ""
        image, _ = self._paths(slug)
        meta = self._meta(slug)
        return meta.get("hash", "") if image.exists() and meta.get("content_type") in IMAGE_TYPES else ""

    def get(self, slug):
        if not self.version(slug):
            return None
        image, _ = self._paths(slug)
        try:
            return image.read_bytes(), self._meta(slug)["content_type"]
        except OSError:
            return None

    def refresh(self, slug, urls, now=None):
        """Download the first usable picture when new, changed or stale. Never raises."""
        if not SLUG_RE.match(slug or ""):
            return False
        now = time.time() if now is None else now
        candidates = [url for url in (picture_url(value) for value in urls) if url]
        if not candidates:
            return False
        with self.lock:
            meta = self._meta(slug)
            key = _source_key(candidates[0])
            if not self._due(slug, candidates[0], now):
                return False
            for url in candidates:
                try:
                    data, content_type = self._download(url)
                except requests.RequestException as err:
                    _LOGGER.warning("event=avatar.failed child=%s error_type=%s", slug, type(err).__name__)
                    continue
                except ValueError as err:
                    _LOGGER.warning("event=avatar.failed child=%s reason=%s", slug, err)
                    continue
                self.directory.mkdir(parents=True, exist_ok=True)
                image, meta_path = self._paths(slug)
                atomic_write(image, data)
                atomic_write(meta_path, json.dumps({
                    "content_type": content_type, "source": _source_key(url), "fetched_at": now,
                    "hash": hashlib.sha256(data).hexdigest()[:12],
                }).encode())
                _LOGGER.info("event=avatar.cached child=%s bytes=%d", slug, len(data))
                return True
            self.directory.mkdir(parents=True, exist_ok=True)
            atomic_write(self._paths(slug)[1], json.dumps({
                **meta, "failed_source": key, "failed_at": now, "failed_version": __version__,
            }).encode())
            return False

    def _download(self, url):
        with self.session.get(url, timeout=(10, 30), stream=True, allow_redirects=True) as response:
            if response.status_code != 200:
                raise ValueError(f"http_{response.status_code}")
            if urlsplit(response.url or url).scheme != "https":
                raise ValueError("insecure_redirect")
            declared = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if declared not in IMAGE_TYPES | GENERIC_TYPES:
                raise ValueError(f"unsupported_content_type:{declared[:40]}")
            data = bytearray()
            for chunk in response.iter_content(65536):
                data.extend(chunk)
                if len(data) > MAX_BYTES:
                    raise ValueError("too_large")
            content_type = sniff(bytes(data[:16]))
            if not content_type:
                raise ValueError("not_an_image")
            return bytes(data), content_type
