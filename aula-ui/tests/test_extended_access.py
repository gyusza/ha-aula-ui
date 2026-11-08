"""Context-only children retain exact IDs throughout direct-source attribution."""

import copy
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest

from aula_ui.aula_api import AulaApiError
from aula_ui.direct_source import DirectAulaClient
from aula_ui.feed import fetch_posts
from aula_ui.gallery import profile_context, tag_choices
from test_direct_account import CHILDREN, PROFILES, Session, controller, install, payload


EXTRA = {"id": 1101, "profileId": 103, "userId": 9101, "name": "Ida Example",
         "institutionCode": "S2"}
CONTEXT = {"institutionProfile": {"id": 2001}, "institutions": [
    {"name": "School one", "institutionCode": "S1", "institutionProfileId": 2001,
     "children": [CHILDREN[0]["relations"][0]]},
    {"name": "School two", "institutionCode": "S2", "institutionProfileId": 2101,
     "children": [EXTRA]},
]}


def source_with_context(controller, context=CONTEXT):
    install(controller)
    return DirectAulaClient(controller.account.connection, Session(
        payload({"profiles": PROFILES}), payload(context)))


def test_discovery_merges_context_children_and_deduplicates_regular_access(controller):
    source = source_with_context(controller)
    result = source.discover()
    assert result[:2] == CHILDREN
    assert result[2] == {"profile_id": "103", "name": "Ida Example", "relations": [EXTRA]}
    assert source.profiles[-1]["institutionProfiles"] == [{"id": 2101, "institutionCode": "S2"}]
    mapped = controller.account._mapped_children(result)
    assert [child["slug"] for child in mapped] == ["child_101", "child_102", "child_103"]
    assert mapped[0]["slug"] == controller.config.children[0].slug


def test_same_child_at_two_institutions_keeps_both_relations(controller):
    context = copy.deepcopy(CONTEXT)
    context["institutions"][1]["children"][0] = {**EXTRA, "profileId": 101, "name": "Maja Example"}
    result = source_with_context(controller, context).discover()
    assert len(result) == 2
    assert [child["id"] for child in result[0]["relations"]] == [1001, 1101]


@pytest.mark.parametrize("field", ["id", "profileId", "userId", "name", "institutionCode"])
def test_incomplete_context_identity_is_explicit_and_preserves_existing_account(controller, field):
    context = copy.deepcopy(CONTEXT)
    context["institutions"][1]["children"][0].pop(field)
    if field == "institutionCode":
        context["institutions"][1].pop(field)
    source = source_with_context(controller, context)
    previous = copy.deepcopy(controller.account.connection.value)
    with pytest.raises(AulaApiError, match="incomplete Aula identifiers"):
        source.discover()
    assert controller.account.connection.value == previous


def test_context_failure_does_not_drop_extended_access(controller):
    source = source_with_context(controller)
    source.session = Session(payload({"profiles": PROFILES}), payload(None))
    with pytest.raises(AulaApiError, match="guardian context"):
        source.discover()
    assert controller.account.connection.value["children"]


def test_posts_gallery_and_tag_choices_use_context_relationships(controller, monkeypatch):
    source = source_with_context(controller)
    merged = source.guardian_profiles()
    reads = []

    def get(uri):
        query = parse_qs(uri.lstrip("?"))
        reads.append(query)
        method = query["method"][0]
        data = {
            "profiles.getProfilesByLogin": {"profiles": PROFILES},
            "profiles.getProfileContext": CONTEXT,
            "posts.getAllPosts": {"posts": [{
                "id": 1, "title": "Ida update", "content": {"html": "Bring lunch"},
                "ownerProfile": {"fullName": "Teacher"},
            }]},
        }[method]
        return {"status": {"code": 0}, "data": data}

    monkeypatch.setattr(source, "get", get)
    child = SimpleNamespace(slug="ida", name="Ida", aula_child_id="103", aula_name="Ida Example")
    posts = fetch_posts(source, [child], 1)
    query = next(read for read in reads if read["method"] == ["posts.getAllPosts"])
    assert query["institutionProfileIds[]"] == ["2101", "1101"]
    assert posts[0]["children"] == ["Ida"] and posts[0]["child_slugs"] == ["ida"]
    assert profile_context(source, [child]) == (["1101", "2101"], {"ida": {"S2"}})
    assert tag_choices(source, [child], {"creator": {"institutionCode": "S2"}}) == [{
        "slug": "ida", "name": "Ida", "institution_profile_id": "1101",
    }]
    assert merged[-1]["children"] == [EXTRA]


def test_missing_guardian_id_does_not_make_empty_gallery_look_successful(controller, monkeypatch):
    context = copy.deepcopy(CONTEXT)
    context["institutions"][1].pop("institutionProfileId")
    source = source_with_context(controller, context)
    merged = source.guardian_profiles()
    monkeypatch.setattr(source, "guardian_profiles", lambda: merged)
    child = SimpleNamespace(slug="ida", name="Ida", aula_child_id="103", aula_name="Ida Example")
    with pytest.raises(AulaApiError, match="No guardian/child"):
        profile_context(source, [child])
    with pytest.raises(AulaApiError, match="No guardian/child"):
        fetch_posts(source, [child], 1)


def test_gallery_matching_never_falls_back_to_name_after_profile_id_mismatch(controller):
    source = source_with_context(controller)
    source.guardian_profiles()
    source.session = Session(payload({"profiles": PROFILES}), payload(CONTEXT))
    child = SimpleNamespace(slug="other", name="Ida", aula_child_id="999", aula_name="Ida Example")
    assert profile_context(source, [child]) == ([], {})
