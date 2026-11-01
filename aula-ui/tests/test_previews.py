"""Preview bytes persist privately without caching failures or original downloads."""

import os
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from aula_ui import previews
from aula_ui.previews import PreviewCache, source_key
from aula_ui.web import start_server

PNG = b"\x89PNG\r\n\x1a\nfixture"


def test_private_cache_survives_restart_and_signed_url_rotation(tmp_path):
    key = source_key("https://files.aula.dk/image.png?token=first")
    assert key == source_key("https://files.aula.dk/image.png?token=second")
    load = Mock(return_value=(PNG, "application/octet-stream"))
    assert PreviewCache(tmp_path).get_or_load(key, load) == (PNG, "image/png")
    assert PreviewCache(tmp_path).get_or_load(key, load) == (PNG, "image/png")
    assert load.call_count == 1
    path = next((tmp_path / "image-previews").glob("*.img"))
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert "token" not in path.name


def test_expiry_failures_and_invalid_bytes_are_not_successfully_cached(tmp_path):
    cache = PreviewCache(tmp_path)
    load = Mock(return_value=(PNG, "image/png"))
    cache.get_or_load("image", load)
    path = next(cache.directory.glob("*.img"))
    os.utime(path, (0, 0))
    load.side_effect = ValueError("Aula unavailable")
    with pytest.raises(ValueError, match="Aula unavailable"):
        cache.get_or_load("image", load)
    load.side_effect = None
    load.return_value = (b"<html>Error</html>", "image/png")
    with pytest.raises(ValueError, match="image preview"):
        cache.get_or_load("other", load)
    assert len(list(cache.directory.glob("*.img"))) == 1


def test_cache_bounds_and_parallel_requests(tmp_path, monkeypatch):
    monkeypatch.setattr(previews, "MAX_BYTES", len(PNG) * 2)
    monkeypatch.setattr(previews, "MAX_ENTRIES", 2)
    cache = PreviewCache(tmp_path)
    load = Mock(return_value=(PNG, "image/png"))
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(lambda _: cache.get_or_load("same", load), range(8))) == [(PNG, "image/png")] * 8
    assert load.call_count == 1
    for key in ("second", "third", "fourth"):
        cache.get_or_load(key, load)
    paths = list(cache.directory.glob("*.img"))
    assert len(paths) <= 2
    assert sum(path.stat().st_size for path in paths) <= len(PNG) * 2


def test_post_preview_reuses_bytes_but_original_stays_fresh(tmp_path, monkeypatch):
    item = {"id": "post:42", "attachments": []}
    controller = SimpleNamespace(
        config=SimpleNamespace(data_dir=str(tmp_path)),
        store=SimpleNamespace(get=lambda *args: [item]), source_client=object())
    fresh = Mock(return_value={"data": {"attachments": [{"media": {
        "mediaType": "image", "file": {"name": "Trip.png", "url": "https://files.aula.dk/trip.png?token=private"},
    }}]}})
    download = Mock(return_value=(PNG, "image/png"))
    monkeypatch.setattr("aula_ui.web._call", fresh)
    monkeypatch.setattr("aula_ui.web.download_attachment", download)
    server = start_server(controller, port=0, allowed_ip="127.0.0.1")
    url = f"http://127.0.0.1:{server.server_port}/api/feed/attachment"
    params = {"id": "post:42", "index": "0", "preview": "1", "source": "https://files.aula.dk/trip.png"}
    try:
        for _ in range(2):
            response = requests.get(url, params=params, timeout=5)
            assert response.status_code == 200 and response.content == PNG
            assert response.headers["Cache-Control"].startswith("private")
        assert fresh.call_count == download.call_count == 1
        assert requests.get(url, params={**params, "source": "https://files.aula.dk/wrong"}, timeout=5).status_code == 502
        assert download.call_count == 1
        response = requests.get(url, params={"id": "post:42", "index": "0", "download": "1"}, timeout=5)
        assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
        assert download.call_count == 2
        assert requests.get(url, params={**params, "id": "post:missing"}, timeout=5).status_code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_album_previews_reuse_cache_without_aula_metadata_requests(tmp_path, monkeypatch):
    controller = SimpleNamespace(config=SimpleNamespace(data_dir=str(tmp_path), children=[]),
                                 source_client=object(), thread_source=object())
    resolve = Mock(return_value=("https://files.aula.dk/trip.png", "Trip.png"))
    download = Mock(return_value=(PNG, "image/png"))
    monkeypatch.setattr("aula_ui.web.gallery.media_url", resolve)
    cover = Mock(return_value="https://files.aula.dk/cover.png")
    monkeypatch.setattr("aula_ui.web.gallery.cover_url", cover)
    monkeypatch.setattr("aula_ui.web.download_attachment", download)
    server = start_server(controller, port=0, allowed_ip="127.0.0.1")
    url = f"http://127.0.0.1:{server.server_port}/api/gallery/image"
    try:
        for _ in range(2):
            assert requests.get(url, params={"album": "77", "media": "1", "size": "small"}, timeout=5).content == PNG
        assert resolve.call_count == download.call_count == 1
        assert requests.get(url, params={"album": "77", "media": "1", "size": "original"}, timeout=5).content == PNG
        assert resolve.call_count == download.call_count == 2
        for _ in range(2):
            assert requests.get(url, params={"album": "77", "cover": "0", "v": "first"}, timeout=5).content == PNG
        assert cover.call_count == 1
        assert requests.get(url, params={"album": "77", "cover": "0", "v": "updated"}, timeout=5).content == PNG
        assert cover.call_count == 2
        assert requests.get(url, params={"album": "77", "media": "1", "size": "invalid"}, timeout=5).status_code == 502
    finally:
        server.shutdown()
        server.server_close()


def test_cached_previews_still_require_app_authentication(tmp_path):
    cache = PreviewCache(tmp_path)
    cache.get_or_load("album:77:1:small::", lambda: (PNG, "image/png"))
    controller = SimpleNamespace(config=SimpleNamespace(data_dir=str(tmp_path)))
    server = start_server(controller, port=0, allowed_ip="192.0.2.1")
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        for path in ("/api/gallery/image?album=77&media=1&size=small",
                     "/api/feed/attachment?id=post:42&index=0&preview=1"):
            assert requests.get(url + path, timeout=5).status_code == 403
    finally:
        server.shutdown()
        server.server_close()
