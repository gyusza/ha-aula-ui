import json
from unittest.mock import Mock

import pytest
import requests

from aula_ui.related_links import (KEY, RelatedReading, bing_search, duckduckgo_search, fingerprint, public_url,
                                   web_search)
from test_controller import FakeHA, build_controller


@pytest.mark.parametrize("url", [
    "http://example.org/article", "https://user@example.org/test",
    "https://127.0.0.1/test", "https://192.168.1.1/test",
    "https://service.local/test", "javascript:alert(1)",
])
def test_search_result_url_validation(url):
    with pytest.raises(ValueError):
        public_url(url)


def test_search_collects_bounded_candidates_no_article_fetch(monkeypatch):
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.status_code = 200
    response.iter_content.return_value = [(
        "<rss><channel>" + "".join(
            f"<item><title>Article {i}</title><link>https://example.org/article/{i}</link></item>"
            for i in range(12)) + "</channel></rss>").encode()]
    get = Mock(return_value=response)
    monkeypatch.setattr("aula_ui.related_links.requests.get", get)
    links = bing_search("public topic")
    assert len(links) == 8 and links[0]["title"] == "Article 0"
    get.assert_called_once_with("https://www.bing.com/search",
                                params={"q": "public topic", "format": "rss", "mkt": "da-DK"},
                                timeout=(5, 20), allow_redirects=False, stream=True)


def _response(body):
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.status_code = 200
    response.iter_content.return_value = [body.encode()]
    return response


def test_duckduckgo_results_parsed_skipping_ads_and_homepages(monkeypatch):
    def result(url, title, ad=False):
        target = "https%3A%2F%2F" + url.replace("/", "%2F")
        return (f'<div class="result results_links{" result--ad" if ad else ""}">'
                f'<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg={target}&amp;rut=x">{title}</a>'
                f'<a class="result__snippet" href="#">Om <b>bevægelse</b></a></div>')
    page = (result("ads.example/buy", "Ad", True) + result("sst.dk/", "Homepage")
            + result("sst.dk/boern/bevaegelse", "Børn &amp; bevægelse") + result("10.0.0.1/x", "Private"))
    monkeypatch.setattr("aula_ui.related_links.requests.post", Mock(return_value=_response(page)))
    links = duckduckgo_search("bevægelse børn")
    assert links == [{"url": "https://sst.dk/boern/bevaegelse", "title": "Børn & bevægelse", "description": "",
                      "snippet": "Om bevægelse", "publisher": "sst.dk"}]


def test_brave_results_parsed_and_preferred(monkeypatch):
    def result(url, title, kind="web"):
        return (f'<div class="snippet svelte-x" data-pos="0" data-type="{kind}"><div><a href="{url}" '
                f'target="_self" class="l1"><div class="title search-snippet-title svelte-y" title="{title}">'
                f'{title}</div></a><div class="generic-snippet svelte-z"><div class="content"> Om '
                f'<strong>lukkedage</strong> </div></div></div>')
    page = (result("https://ad.example/x", "Ad", "ad") + result("https://kk.dk/", "Home")
            + result("https://kk.dk/lukkedage", "Lukkedage &amp; regler"))
    get = Mock(return_value=_response(page))
    post = Mock()
    monkeypatch.setattr("aula_ui.related_links.requests.get", get)
    monkeypatch.setattr("aula_ui.related_links.requests.post", post)
    assert web_search("lukkedage") == [{"url": "https://kk.dk/lukkedage", "title": "Lukkedage & regler",
                                        "description": "", "snippet": "Om lukkedage", "publisher": "kk.dk"}]
    assert get.call_args.args[0] == "https://search.brave.com/search"
    post.assert_not_called()


def test_search_falls_back_through_engines(monkeypatch):
    bing = _response("<rss><channel><item><title>Fallback</title>"
                     "<link>https://example.org/a</link></item></channel></rss>")
    monkeypatch.setattr("aula_ui.related_links.requests.get",
                        Mock(side_effect=[requests.ConnectionError("brave down"), bing]))
    monkeypatch.setattr("aula_ui.related_links.requests.post", Mock(return_value=Mock(
        __enter__=Mock(return_value=Mock(status_code=202, raise_for_status=Mock())),
        __exit__=Mock(return_value=False))))
    assert [link["title"] for link in web_search("emne")] == ["Fallback"]


def test_manual_topic_search_cached_without_feed_changes(tmp_path, monkeypatch):
    controller = build_controller(tmp_path, FakeHA())
    item = {"id": "test", "content_type": "Post", "title": "Private message"}
    controller.store.set("feed", [item])
    controller.provider = Mock()
    controller.provider._generate.side_effect = [
        {"links_json": '[{"query":"cykelhjelm børn Danmark sikkerhed"}]'},
        {"links_json": '[{"url":"https://www.sikkertrafik.dk/raad-og-viden/"}]'},
    ]
    links = [{"url": "https://www.sikkertrafik.dk/raad-og-viden/", "title": "Cycling advice"}]
    search = Mock(return_value=links)
    monkeypatch.setattr("aula_ui.related_links.web_search", search)
    reading = RelatedReading(controller)
    assert reading.snapshot(item)["status"] == "idle"
    search.assert_not_called()
    reading._research(item)
    search.assert_called_once_with("cykelhjelm børn Danmark sikkerhed")
    assert reading.snapshot(item)["links"] == links
    assert controller.provider._generate.call_count == 2
    assert controller.store.get("feed") == [item]
    controller.store.load()
    assert RelatedReading(controller).snapshot(item)["status"] == "ready"
    changed = reading.snapshot({**item, "title": "Changed"})
    assert changed["status"] == "ready" and changed["links"] == links and changed["outdated"] is True
    assert reading.snapshot(item)["outdated"] is False
    controller.close()


def test_web_search_ai_task_returns_verified_links(tmp_path, monkeypatch):
    controller = build_controller(tmp_path, FakeHA())
    item = {"id": "grounded", "content_type": "Post", "title": "Cykeltur i skoven"}
    controller.provider = Mock()
    grounded = Mock()
    grounded._generate.return_value = {"links_json": json.dumps([
        {"url": "https://www.sikkertrafik.dk/cykel/boern", "title": "Børn på cykel", "publisher": "Rådet",
         "description": "Safety advice."},
        {"url": "https://www.sikkertrafik.dk/cykel/boern", "title": "Duplicate"},
        {"url": "https://dead.example/page", "title": "Invented"},
    ])}
    controller.reading_provider = Mock(return_value=grounded)
    search = Mock()
    monkeypatch.setattr("aula_ui.related_links.web_search", search)
    monkeypatch.setattr("aula_ui.related_links.reachable", lambda url: "dead" not in url)
    reading = RelatedReading(controller)
    reading._research(item)
    assert reading.snapshot(item)["links"] == [{"url": "https://www.sikkertrafik.dk/cykel/boern",
                                                "title": "Børn på cykel", "publisher": "Rådet",
                                                "description": "Safety advice."}]
    search.assert_not_called()
    controller.provider._generate.assert_not_called()
    controller.close()


def test_grounded_links_drop_homepages_aula_and_invalid_urls(monkeypatch):
    from aula_ui.related_links import grounded_links
    provider = Mock()
    provider._generate.side_effect = [{"links_json": json.dumps([
        {"url": "https://www.sst.dk/", "title": "Home"},
        {"url": "https://www.aula.dk/artikel", "title": "Aula"},
        {"url": "http://example.org/insecure", "title": "Plain HTTP"},
    ])}, {"links_json": json.dumps([
        {"url": "https://192.168.1.10/x", "title": "Private"},
        {"url": "https://example.org/emne", "title": "  Godt   emne "},
    ])}]
    monkeypatch.setattr("aula_ui.related_links.reachable", lambda url: True)
    assert grounded_links(provider, "ctx") == []
    assert grounded_links(provider, "ctx") == [{"url": "https://example.org/emne", "title": "Godt emne",
                                                "publisher": "example.org", "description": ""}]


def test_reachable_rejects_private_hosts_and_missing_pages(monkeypatch):
    from aula_ui import related_links
    monkeypatch.setattr(related_links.socket, "getaddrinfo",
                        lambda host, *args, **kwargs: [(0, 0, 0, "", ("10.0.0.5" if host == "intra.example" else "93.184.216.34", 443))])
    get = Mock()
    monkeypatch.setattr(related_links.requests, "get", get)
    assert related_links.reachable("https://intra.example/x") is False
    get.assert_not_called()
    for status, expected in ((200, True), (403, True), (404, False), (500, False)):
        response = Mock(status_code=status)
        get.return_value = Mock(__enter__=Mock(return_value=response), __exit__=Mock(return_value=False))
        assert related_links.reachable("https://example.org/x") is expected


@pytest.mark.parametrize("content_type", ["Post", "Message", "Calendar", "Weekplan", "Homework"])
def test_routine_items_of_any_type_are_searched_on_request(tmp_path, monkeypatch, content_type):
    controller = build_controller(tmp_path, FakeHA())
    controller.provider = Mock()
    controller.provider._generate.side_effect = [
        {"links_json": '[{"query":"naturlegeplads børn udeliv"}]'},
        {"links_json": '[{"url":"https://example.org/udeliv"}]'},
    ]
    search = Mock(return_value=[{"url": "https://example.org/udeliv", "title": "Udeliv", "snippet": ""}])
    monkeypatch.setattr("aula_ui.related_links.web_search", search)
    reading = RelatedReading(controller)
    item = {"id": "test", "content_type": content_type, "title": "Thursday: play, dance and a walk to the lake"}
    reading._research(item)
    search.assert_called_once_with("naturlegeplads børn udeliv")
    assert [link["url"] for link in reading.snapshot(item)["links"]] == ["https://example.org/udeliv"]
    prompt = controller.provider._generate.call_args_list[0].args[0]
    assert "explicitly asked" in prompt and "ordinary daily activity reports" not in prompt
    controller.close()


def test_results_kept_until_explicit_regenerate(tmp_path, monkeypatch):
    controller = build_controller(tmp_path, FakeHA())
    item = {"id": "test", "content_type": "Message", "title": "Lus"}
    search = Mock(side_effect=[
        [{"url": "https://www.sundhed.dk/lus/1", "title": "Lus behandling"}],
        [{"url": "https://www.sundhed.dk/lus/2", "title": "Lus kam"}],
    ])
    monkeypatch.setattr("aula_ui.related_links.web_search", search)
    reading = RelatedReading(controller)
    assert reading.request(item)["status"] == "pending"
    assert reading.wait(item, timeout=5)["links"][0]["url"].endswith("/1")
    assert reading.request(item)["links"][0]["url"].endswith("/1")
    assert search.call_count == 1
    assert reading.request(item, regenerate=True)["status"] == "pending"
    assert reading.wait(item, timeout=5)["links"][0]["url"].endswith("/2")
    assert search.call_count == 2
    controller.close()


def test_unrelated_results_rejected_and_old_ready_cache_hidden(tmp_path, monkeypatch):
    controller = build_controller(tmp_path, FakeHA())
    item = {"id": "test", "title": "Thursday at daycare"}
    controller.store.set(KEY, {"test": {"status": "ready", "fingerprint": fingerprint(item),
                                       "links": [{"url": "https://www.sst.dk/"}]}})
    reading = RelatedReading(controller)
    assert reading.snapshot(item) == {"status": "idle", "links": []}
    controller.provider = Mock()
    controller.provider._generate.side_effect = [
        {"links_json": '[{"query":"børn udendørs aktiviteter"}]'},
        {"links_json": "[]"},
    ]
    monkeypatch.setattr("aula_ui.related_links.web_search", Mock(return_value=[
        {"url": "https://www.sst.dk/", "title": "Sundhedsstyrelsen", "snippet": "Authority homepage"},
        {"url": "https://www.sst.dk/vaccination", "title": "Vaccination", "snippet": "Vaccines"},
    ]))
    reading._research(item)
    assert reading.snapshot(item)["status"] == "ready"
    assert reading.snapshot(item)["links"] == []
    controller.close()


def test_legacy_errors_remain_stopped_after_relevance_upgrade(tmp_path):
    controller = build_controller(tmp_path, FakeHA())
    item = {"id": "test", "title": "Topic"}
    controller.store.set(KEY, {"test": {"status": "error", "fingerprint": fingerprint(item),
                                       "links": [], "error": "Stopped"}})
    assert RelatedReading(controller).snapshot(item)["status"] == "error"
    controller.close()


def test_lice_search_excludes_generic_vaccination_results(tmp_path, monkeypatch):
    controller = build_controller(tmp_path, FakeHA())
    item = {"id": "test", "title": "Lus", "content_type": "Message"}
    monkeypatch.setattr("aula_ui.related_links.web_search", Mock(return_value=[
        {"url": "https://www.sst.dk/vaccination/", "title": "Vaccination"},
        *[{"url": f"https://www.sundhed.dk/lus/{i}", "title": "Lus behandling",
           "snippet": "Combing advice"} for i in range(5)],
    ]))
    reading = RelatedReading(controller)
    reading._research(item)
    links = reading.snapshot(item)["links"]
    assert len(links) == 3
    assert all("vaccination" not in link["url"] and "snippet" not in link for link in links)
    controller.close()


def test_selector_cannot_invent_links(tmp_path, monkeypatch):
    controller = build_controller(tmp_path, FakeHA())
    controller.provider = Mock()
    controller.provider._generate.side_effect = [
        {"links_json": '[{"query":"cykelhjelm sikkerhed børn"}]'},
        {"links_json": '[{"url":"https://invented.example/article"},{"url":"https://example.org/cycling"}]'},
    ]
    monkeypatch.setattr("aula_ui.related_links.web_search", Mock(return_value=[
        {"url": "https://example.org/cycling", "title": "Cycling"}]))
    reading = RelatedReading(controller)
    item = {"id": "test", "title": "Advice"}
    reading._research(item)
    assert reading.snapshot(item)["status"] == "ready"
    assert [link["url"] for link in reading.snapshot(item)["links"]] == ["https://example.org/cycling"]
    controller.close()


def test_failure_logged_persisted_never_retried_by_status(tmp_path, monkeypatch, caplog):
    controller = build_controller(tmp_path, FakeHA())
    item = {"id": "test", "content_type": "Message", "title": "Lus"}
    search = Mock(side_effect=requests.Timeout())
    monkeypatch.setattr("aula_ui.related_links.web_search", search)
    reading = RelatedReading(controller)
    reading._research(item)
    for _ in range(5):
        assert reading.snapshot(item)["status"] == "error"
    assert search.call_count == 1
    assert "event=related_reading.failed error_type=Timeout" in caplog.text
    controller.store.load()
    assert RelatedReading(controller).snapshot(item)["status"] == "error"
    assert search.call_count == 1
    controller.close()


@pytest.mark.parametrize("query", ["https://private.example", "call me 12345", "private@example.org"])
def test_invalid_topic_not_sent_to_search(tmp_path, monkeypatch, query):
    controller = build_controller(tmp_path, FakeHA())
    controller.provider = Mock()
    controller.provider._generate.return_value = {"links_json": json.dumps([{"query": query}])}
    search = Mock()
    monkeypatch.setattr("aula_ui.related_links.web_search", search)
    reading = RelatedReading(controller)
    item = {"id": "test", "content_type": "Post", "title": "Topic"}
    reading._research(item)
    assert reading.snapshot(item)["status"] == "error"
    search.assert_not_called()
    controller.close()


def test_api_lookup_manual_and_requires_header(tmp_path, monkeypatch):
    from aula_ui.web import start_server
    controller = build_controller(tmp_path, FakeHA())
    controller.store.set("feed", [{"id": "lice", "title": "Lus", "content_type": "Message"},
                                  {"id": "event", "title": "Skovtur", "content_type": "Calendar"}])
    search = Mock(return_value=[])
    monkeypatch.setattr("aula_ui.related_links.web_search", search)
    server = start_server(controller, port=0, allowed_ip="127.0.0.1")
    url = f"http://127.0.0.1:{server.server_port}/api/feed/reading"
    try:
        assert requests.get(url, params={"id": "lice"}, timeout=5).json()["status"] == "idle"
        search.assert_not_called()
        assert requests.post(url, json={"id": "lice"}, timeout=5).status_code == 403
        response = requests.post(url, json={"id": "lice"}, headers={"X-Aula-Request": "settings"}, timeout=5)
        assert response.json()["status"] == "pending"
        assert requests.get(url, params={"id": "lice", "wait": "1"}, timeout=10).json()["status"] == "ready"
        assert requests.get(url, params={"id": "missing"}, timeout=5).status_code == 404
        headers = {"X-Aula-Request": "settings"}
        assert requests.post(url, json={"id": "lice", "regenerate": "yes"}, headers=headers,
                             timeout=5).status_code == 400
        assert requests.post(url, json={"id": "lice", "extra": 1}, headers=headers, timeout=5).status_code == 400
        assert requests.post(url, json={"id": "lice"}, headers=headers, timeout=5).json()["status"] == "ready"
        assert search.call_count == 1
        assert requests.post(url, json={"id": "lice", "regenerate": True}, headers=headers,
                             timeout=5).json()["status"] == "pending"
        assert requests.get(url, params={"id": "lice", "wait": "1"}, timeout=10).json()["status"] == "ready"
        assert search.call_count == 2
        assert requests.get(url, params={"id": "event"}, timeout=5).json()["status"] == "idle"
        controller.feed_json = json.dumps({"items": [
            {"id": "calendar:abc", "title": "Juletræsfest", "content_type": "Calendar", "source": "calendar"},
            {"id": "presence:x", "title": "Nap", "content_type": "Presence"}]}).encode()
        controller.provider = Mock()
        controller.provider._generate.return_value = {"links_json": '[{"query":"juletræsfest børn"}]'}
        assert requests.get(url, params={"id": "calendar:abc"}, timeout=5).json()["status"] == "idle"
        assert requests.post(url, json={"id": "calendar:abc"}, headers=headers,
                             timeout=5).json()["status"] == "pending"
        assert requests.get(url, params={"id": "calendar:abc", "wait": "1"}, timeout=10).json()["status"] == "ready"
        assert requests.get(url, params={"id": "presence:x"}, timeout=5).status_code == 404
    finally:
        server.shutdown()
        server.server_close()
        controller.close()
