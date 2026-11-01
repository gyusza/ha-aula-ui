"""Bounded, private disk cache for same-origin image previews."""

import hashlib
import logging
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from .auth_storage import atomic_write
from .avatars import sniff

_LOGGER = logging.getLogger(__name__)
TTL = 7 * 24 * 3600
MAX_BYTES = 128 * 1024 * 1024
MAX_ENTRIES = 512


def source_key(url):
    """Signed credentials expire; the media object path stays stable."""
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


class PreviewCache:
    def __init__(self, data_dir):
        self.directory = Path(data_dir) / "image-previews"
        self.lock = threading.Lock()
        self.loads = [threading.Lock() for _ in range(16)]

    def _prune(self, now):
        entries = []
        for path in self.directory.glob("*.img"):
            stat = path.stat()
            if now - stat.st_mtime >= TTL:
                path.unlink()
            else:
                entries.append((stat.st_mtime, path, stat.st_size))
        size = sum(entry[2] for entry in entries)
        count = len(entries)
        for _, path, length in sorted(entries):
            if size <= MAX_BYTES and count <= MAX_ENTRIES:
                break
            path.unlink()
            size -= length
            count -= 1

    def get_or_load(self, key, load):
        digest = hashlib.sha256(key.encode()).hexdigest()
        path = self.directory / f"{digest}.img"
        with self.loads[int(digest[:2], 16) % len(self.loads)]:
            with self.lock:
                try:
                    if path.exists() and time.time() - path.stat().st_mtime < TTL:
                        data = path.read_bytes()
                        content_type = sniff(data[:16])
                        if content_type:
                            return data, content_type
                        path.unlink()
                except OSError:
                    _LOGGER.warning("event=preview.cache_read_failed")
            data, _ = load()
            content_type = sniff(data[:16])
            if not content_type:
                raise ValueError("Aula did not return an image preview")
            with self.lock:
                try:
                    self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                    atomic_write(path, data)
                    self._prune(time.time())
                except OSError:
                    _LOGGER.warning("event=preview.cache_write_failed")
            return data, content_type
