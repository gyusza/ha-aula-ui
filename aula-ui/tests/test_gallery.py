from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest

from aula_ui import gallery

PROFILES = [{
    "children": [{"id": 20, "profileId": 2, "name": "Maja Example", "institutionCode": "A1"}],
    "institutionProfiles": [{"id": 10, "institutionCode": "A1"}],
}]
CHILD = SimpleNamespace(name="Maja", slug="maja", aula_name="Maja Example", aula_child_id="2")
OTHER = SimpleNamespace(name="Ida", slug="ida", aula_name="Ida Example", aula_child_id="3")
ALBUM = {
    "id": 77, "title": " Cykel tur ", "description": "Vi cyklede", "creationDate": "2026-10-01T08:00:00+00:00",
    "creator": {"name": "Teacher", "institutionCode": "A1"},
    "sharedWithGroups": [{"name": "Stue 1", "institutionCode": "A1"}],
    "thumbnailsUrls": ["https://media-prod.aula.dk/a_200x200.jpeg"] * 2,
}


def photo(number, kind="image", deleted=False):
    return {"id": number, "mediaType": kind, "isDeleted": deleted,
            "file": {"url": f"https://media-prod.aula.dk/{number}.jpg", "name": f"{number}.jpg", "created": "x"},
            "smallThumbnailUrl": f"https://media-prod.aula.dk/{number}_small.jpg",
            "largeThumbnailUrl": f"https://media-prod.aula.dk/{number}_large.jpg"}


class Client:
    def __init__(self, media=None):
        self.queries = []
        self.media = media if media is not None else [photo(1), photo(2, "video"), photo(3, deleted=True)]

    def call_service(self, domain, service, data=None, **kwargs):
        query = parse_qs(data["uri"].lstrip("?"))
        self.queries.append(query)
        method = query["method"][0]
        if method == "profiles.getProfilesByLogin":
            payload = {"profiles": PROFILES}
        elif method == "gallery.getAlbums":
            payload = [ALBUM, {"id": None, "title": "Virtual"}]
        else:
            start = int(query["index"][0])
            payload = {"album": {"title": "Cykel tur"}, "mediaCount": len(self.media),
                       "results": self.media[start:start + 100]}
        return {"service_response": {"status": {"code": 0}, "data": payload}}


@pytest.fixture(autouse=True)
def clear_cache():
    gallery._cache.clear()


def test_feed_items_attribute_albums_to_the_matching_child_only():
    items = gallery.feed_items(Client(), [CHILD, OTHER])
    assert [item["id"] for item in items] == ["album:77"]
    item = items[0]
    assert item["title"] == "Cykel tur" and item["content_type"] == "Album"
    assert item["child_slugs"] == ["maja"] and item["album"]["thumbs"] == 2
    assert item["album"]["groups"] == ["Stue 1"]


def test_requests_carry_guardian_and_child_profiles():
    client = Client()
    gallery.feed_items(client, [CHILD])
    albums = next(query for query in client.queries if query["method"] == ["gallery.getAlbums"])
    assert albums["filterInstProfileIds[]"] == ["20", "10"]


def test_album_detail_skips_deleted_media_and_keeps_videos():
    detail = gallery.album_detail(Client(), [CHILD], "77")
    assert [(row["id"], row["type"]) for row in detail["media"]] == [("1", "image"), ("2", "video")]


def test_media_url_picks_requested_size_and_rejects_unknown_ones():
    client = Client()
    assert gallery.media_url(client, [CHILD], "77", "1", "large")[0].endswith("1_large.jpg")
    assert gallery.media_url(client, [CHILD], "77", "1", "original") == ("https://media-prod.aula.dk/1.jpg", "1.jpg")
    with pytest.raises(ValueError):
        gallery.media_url(client, [CHILD], "77", "1", "huge")
    with pytest.raises(LookupError):
        gallery.media_url(client, [CHILD], "77", "999", "small")


def test_media_is_paged_until_complete():
    client = Client([photo(number) for number in range(1, 151)])
    assert len(gallery.album_detail(client, [CHILD], "77")["media"]) == 150
    assert sum(query["method"] == ["gallery.getMedia"] for query in client.queries) == 2


def test_cover_url_bounds():
    client = Client()
    gallery.feed_items(client, [CHILD])
    assert gallery.cover_url(client, [CHILD], "77", 1).startswith("https://media-prod.aula.dk/")
    with pytest.raises(LookupError):
        gallery.cover_url(client, [CHILD], "77", 5)


class TagClient(Client):
    def __init__(self):
        super().__init__([{
            **photo(1), "currentUserCanEditTags": True,
            "tags": [{"id": 900, "profileId": 901}],
        }])
        self.writes = []
        self.accept = True
        self.persist = True

    def call_service(self, domain, service, data=None, **kwargs):
        payload = super().call_service(domain, service, data, **kwargs)
        if parse_qs(data["uri"].lstrip("?"))["method"] == ["gallery.getMedia"]:
            payload["service_response"]["data"]["album"] = ALBUM
        return payload

    def post(self, uri, data):
        self.writes.append((uri, data))
        if self.accept and self.persist:
            tags = self.media[0]["tags"]
            if "addTag" in uri:
                tags.append({"id": data["instProfileId"], "profileId": 2})
            else:
                self.media[0]["tags"] = [tag for tag in tags if tag["id"] != data["instProfileId"]]
        return {"status": {"code": 0}, "data": self.accept}


def test_album_reports_tag_permissions_and_configured_children_only():
    client = TagClient()
    client.media[0]["tags"].append({"id": 20, "profileId": 2})
    detail = gallery.album_detail(client, [CHILD, OTHER], "77")
    assert detail["tag_choices"] == [{"slug": "maja", "name": "Maja"}]
    assert detail["media"][0]["can_edit_tags"] is True
    assert detail["media"][0]["tagged_children"] == ["maja"]


def test_tag_add_remove_are_confirmed_and_preserve_other_people():
    client = TagClient()
    result = gallery.set_child_tag(client, [CHILD], "77", "1", "maja", True)
    assert result["tagged_children"] == ["maja"]
    assert client.writes == [("?method=gallery.addTag", {"mediaId": 1, "instProfileId": 20})]
    gallery.set_child_tag(client, [CHILD], "77", "1", "maja", True)
    assert len(client.writes) == 1
    result = gallery.set_child_tag(client, [CHILD], "77", "1", "maja", False)
    assert result["tagged_children"] == []
    assert client.media[0]["tags"] == [{"id": 900, "profileId": 901}]


def test_tag_requires_permission_and_exact_configured_child():
    client = TagClient()
    with pytest.raises(ValueError):
        gallery.set_child_tag(client, [CHILD], "77", "1", "stranger", True)
    with pytest.raises(LookupError):
        gallery.set_child_tag(client, [CHILD], "77", "999", "maja", True)
    client.media[0]["currentUserCanEditTags"] = False
    with pytest.raises(PermissionError):
        gallery.set_child_tag(client, [CHILD], "77", "1", "maja", True)
    assert not client.writes


@pytest.mark.parametrize("album,media,child,tagged", [
    (77, "1", "maja", True), ("77", "../1", "maja", True),
    ("77", "1", "maja", "true"), ("77", "1", None, True),
])
def test_tag_rejects_invalid_input(album, media, child, tagged):
    client = TagClient()
    with pytest.raises(ValueError):
        gallery.set_child_tag(client, [CHILD], album, media, child, tagged)
    assert not client.writes


@pytest.mark.parametrize("accept,persist", [(False, False), (True, False)])
def test_tag_does_not_report_success_without_persisted_change(accept, persist):
    from aula_ui.aula_api import AulaApiError

    client = TagClient()
    client.accept, client.persist = accept, persist
    with pytest.raises(AulaApiError):
        gallery.set_child_tag(client, [CHILD], "77", "1", "maja", True)
