import json

import pytest

from aula_ui.avatars import MAX_BYTES, Avatars, picture_url
from test_controller import TWO_CHILDREN, FakeHA, build_controller

PNG = b"\x89PNG\r\n\x1a\nfake-image"


class Response:
    def __init__(self, body=PNG, status=200, content_type="image/png", url=None):
        self.body, self.status_code, self.url = body, status, url
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def iter_content(self, size):
        for start in range(0, len(self.body), size):
            yield self.body[start:start + size]


class Session:
    def __init__(self, *responses):
        self.responses, self.urls = list(responses), []

    def get(self, url, **kwargs):
        self.urls.append(url)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        response.url = response.url or url
        return response


def url(path="kids/noah.jpg", sig="a"):
    return f"https://media-prod.aula.dk/{path}?X-Amz-Signature={sig}"


def test_picture_url_accepts_aula_shapes_and_rejects_unsafe():
    assert picture_url({"profilePicture": {"url": url()}}) == url()
    assert picture_url({"institutionProfile": {"profilePicture": {"url": url()}}}) == url()
    assert picture_url(url()) == url()
    for bad in ("http://x/y.jpg", "https://user:pw@x/y.jpg", "javascript:alert(1)", None, {"profilePicture": None}):
        assert picture_url(bad) == ""


def test_refresh_caches_image_and_skips_unchanged_until_stale(tmp_path):
    session = Session(Response(), Response(PNG + b"2"))
    avatars = Avatars(tmp_path, session)
    assert avatars.version("noah") == "" and avatars.get("noah") is None
    assert avatars.refresh("noah", [{"profilePicture": {"url": url()}}], now=1000)
    assert avatars.get("noah") == (PNG, "image/png")
    first = avatars.version("noah")
    assert first
    # A new signature for the same object is not a new picture.
    assert not avatars.refresh("noah", [url(sig="b")], now=2000)
    assert len(session.urls) == 1
    assert avatars.refresh("noah", [url(path="kids/new.jpg")], now=3000)
    assert avatars.version("noah") != first
    assert "X-Amz" not in (tmp_path / "avatars" / "noah.json").read_text()


def test_refresh_falls_back_and_rejects_bad_payloads(tmp_path):
    session = Session(Response(content_type="text/html"), Response(status=403),
                      Response(body=b"x" * (MAX_BYTES + 1)), Response())
    avatars = Avatars(tmp_path, session)
    assert avatars.refresh("noah", ["", url("a.jpg"), url("b.jpg"), url("c.jpg"), url("d.jpg")], now=0)
    assert len(session.urls) == 4 and avatars.get("noah")[0] == PNG


def test_failed_download_backs_off_and_bad_slugs_are_ignored(tmp_path):
    session = Session(Response(status=404), Response())
    avatars = Avatars(tmp_path, session)
    assert not avatars.refresh("noah", [url()], now=0)
    assert not avatars.refresh("noah", [url()], now=60)
    assert len(session.urls) == 1
    assert avatars.refresh("noah", [url()], now=7 * 3600)
    assert not avatars.refresh("../etc", [url()], now=0)
    assert avatars.get("../etc") is None
    assert not Avatars(tmp_path, Session()).refresh("alma", [None, {}], now=0)


def test_redirect_to_http_is_rejected(tmp_path):
    avatars = Avatars(tmp_path, Session(Response(url="http://evil.example/x.png")))
    assert not avatars.refresh("noah", [url()], now=0)


def test_feed_exposes_version_and_http_serves_image(tmp_path):
    import requests
    from aula_ui.web import start_server

    controller = build_controller(tmp_path, FakeHA(), children=TWO_CHILDREN)
    controller.avatars.session = Session(Response())
    assert controller.avatars.refresh("noah", [url()], now=0)
    controller.publish_feed()
    children = {child["slug"]: child for child in json.loads(controller.feed_json)["children"]}
    assert children["noah"]["avatar"] == controller.avatars.version("noah")
    assert children["alma"]["avatar"] == ""
    service = start_server(controller, port=0, allowed_ip="127.0.0.1")
    base = f"http://127.0.0.1:{service.server_port}"
    try:
        response = requests.get(base + "/api/avatars/noah?v=1", timeout=5)
        assert response.status_code == 200 and response.content == PNG
        assert response.headers["Content-Type"] == "image/png"
        assert requests.get(base + "/api/avatars/alma", timeout=5).status_code == 404
        assert requests.get(base + "/api/avatars/..%2Fstate", timeout=5).status_code == 404
    finally:
        service.shutdown()
        service.server_close()


@pytest.mark.parametrize("present", [True, False])
def test_collect_refreshes_from_relations_and_presence(tmp_path, present):
    controller = build_controller(tmp_path, FakeHA(), children=TWO_CHILDREN)
    seen = []
    controller.avatars.refresh = lambda slug, pictures: seen.append((slug, list(pictures)))
    from aula_ui.account import AulaAccount

    class Source:
        def discover(self):
            return [{"profile_id": "1", "name": "Noah", "relations": [{"id": 1, "profilePicture": {"url": url()}}]}]

        def presence_many(self, children):
            return {child["profile_id"]: {"picture_url": url("p.jpg"), "status": "Present", "present": True,
                                          "exit_with": "", "date": ""} if present else None
                    for child in children}

        def easyiq_events(self, child, weeks):
            return []

    account = AulaAccount.__new__(AulaAccount)
    account.controller, account.source = controller, Source()
    import threading
    account.lock = threading.RLock()
    account._mapped_children = lambda discovered: [{**discovered[0], "slug": "noah", "display_name": "Noah"}]

    class Connection:
        value = {"username": "u", "tokens": {}}

        def install(self, *args):
            pass

    account.connection = Connection()
    account._apply_children = lambda mapped: None
    account.collect()
    assert seen[0][0] == "noah"
    assert seen[0][1][0]["profilePicture"]["url"] == url()
    if present:
        assert seen[0][1][1] == url("p.jpg")
        assert "picture_url" not in controller.store.get("direct_presence:noah")


def test_generic_content_type_is_sniffed_and_non_images_rejected(tmp_path):
    jpeg = b"\xff\xd8\xff\xe0" + b"0" * 20
    session = Session(Response(body=b"<html>", content_type="binary/octet-stream"),
                      Response(body=jpeg, content_type="binary/octet-stream"))
    avatars = Avatars(tmp_path, session)
    assert avatars.refresh("noah", [url("a.jpg"), url("b.jpg")], now=0)
    assert avatars.get("noah") == (jpeg, "image/jpeg")


def test_failure_backoff_resets_after_upgrade(tmp_path, monkeypatch):
    session = Session(Response(status=403), Response())
    avatars = Avatars(tmp_path, session)
    assert not avatars.refresh("noah", [url()], now=0)
    monkeypatch.setattr("aula_ui.avatars.__version__", "next")
    assert avatars.refresh("noah", [url()], now=60)


def test_author_pictures_are_captured_from_posts_and_messages(tmp_path):
    from aula_ui import avatars as module
    from aula_ui.aula_api import fetch_thread_messages, message_from_thread_summary

    module._pending_authors.clear()

    class Client:
        def call_service(self, domain, service, data, **kwargs):
            return {"service_response": {"data": {"subject": "Trip", "messages": [
                {"id": 1, "text": "Hello", "sendDateTime": "2026-10-01T08:00:00Z",
                 "sender": {"fullName": "Mette Teacher", "profilePicture": {"url": url("mette.jpg")}}},
                {"id": 2, "text": "No picture", "sendDateTime": "2026-10-01T09:00:00Z",
                 "sender": {"fullName": "Bo Parent"}},
            ], "moreMessagesExist": False}}}

    fetch_thread_messages(Client(), 7)
    message_from_thread_summary({"subject": "S", "creator": {"fullName": "Carl Head", "profilePicture": {"url": url("carl.jpg")}},
                                 "latestMessage": {"id": 3, "text": "x", "sender": {"fullName": "Mette Teacher"}}})
    assert set(module._pending_authors) == {"Mette Teacher", "Carl Head"}

    session = Session(Response(), Response())
    avatars = Avatars(tmp_path, session)
    assert avatars.refresh_authors(now=0) == 2
    authors = avatars.authors()
    assert set(authors) == {"Mette Teacher", "Carl Head"}
    assert authors["Mette Teacher"]["key"].startswith("p-") and authors["Mette Teacher"]["v"]
    assert avatars.get(authors["Carl Head"]["key"]) == (PNG, "image/png")
    assert "https" not in (tmp_path / "avatars" / "authors.json").read_text()
    # Fresh pictures are not downloaded again on later syncs.
    module.note_author("Mette Teacher", {"profilePicture": {"url": url("mette.jpg", sig="z")}})
    assert avatars.refresh_authors(now=10) == 0 and len(session.urls) == 2


def test_author_downloads_are_bounded_per_sync(tmp_path):
    from aula_ui import avatars as module

    module._pending_authors.clear()
    for number in range(4):
        module.note_author(f"Person {number}", {"profilePicture": {"url": url(f"{number}.jpg")}})
    module.note_author("", {"profilePicture": {"url": url()}})
    module.note_author("No URL", {"fullName": "No URL"})
    avatars = Avatars(tmp_path, Session(*[Response() for _ in range(4)]))
    assert avatars.refresh_authors(limit=3, now=0) == 3
    assert len(avatars.authors()) == 3
    module.note_author("Person 3", {"profilePicture": {"url": url("3.jpg")}})
    assert avatars.refresh_authors(limit=3, now=1) == 1
    assert len(avatars.authors()) == 4
