"""Tests for message normalisation, using the real payloads from the host."""

from aula_ui.messages import (
    extract_messages,
    looks_non_english,
    message_id,
    normalise_message,
    strip_html,
)

# Captured verbatim from binary_sensor.easyiq_messages on the live instance.
REAL_EASYIQ_STATE = {
    "entity_id": "binary_sensor.easyiq_messages",
    "state": "off",
    "last_changed": "2026-10-01T08:00:00+00:00",
    "attributes": {
        "unread_count": 0,
        "coordinator_available": True,
        "last_update_success": True,
        "subject": "OBS. Vigtig informationer",
        "text": "<div>Her er lidt om Signe</div>",
        "sender": "Freja Holm Mikkelsen",
        "friendly_name": "EasyIQ Messages",
    },
}

# The richer shape documented in REQUIREMENTS.md 3.2.
ALL_LIST_STATE = {
    "entity_id": "binary_sensor.easyiq_messages",
    "state": "on",
    "last_changed": "2026-10-01T08:00:00+00:00",
    "attributes": {
        "all": [
            {
                "id": "12345",
                "subject": "Opgave 3",
                "text": "Følgende opgaver skal afleveres i morgen",
                "sender": "Lærer",
                "date": "2026-09-28T14:07:00+02:00",
            },
            {
                "id": "12344",
                "subject": "Welcome",
                "text": "The school year starts Monday.",
                "sender": "Office",
                "date": "2026-09-20T09:00:00+02:00",
            },
        ]
    },
}


def test_strip_html_produces_plain_text():
    assert strip_html("<div>Her er lidt om Signe</div>") == "Her er lidt om Signe"
    assert strip_html("<p>One</p><p>Two</p>") == "One\nTwo"
    assert strip_html("a<br>b") == "a\nb"
    assert strip_html("<script>evil()</script>ok") == "ok"
    assert strip_html("Caf&eacute; &amp; co") == "Café & co"
    assert strip_html(None) == ""


def test_message_id_is_stable_and_deterministic():
    first = message_id("s", "from", "body")
    assert first == message_id("s", "from", "body")
    assert first != message_id("s", "from", "other body")
    assert first.startswith("msg_")


def test_message_id_prefers_explicit_id():
    assert message_id("s", "f", "t", explicit="42") == "42"
    assert message_id("s", "f", "t", explicit=None).startswith("msg_")
    assert message_id("s", "f", "t", explicit="").startswith("msg_")


def test_extract_messages_from_real_flat_sensor():
    messages = extract_messages(REAL_EASYIQ_STATE)
    assert len(messages) == 1
    message = messages[0]
    assert message["title"] == "OBS. Vigtig informationer"
    assert message["original_text"] == "Her er lidt om Signe"
    assert message["sender"] == "Freja Holm Mikkelsen"
    assert message["date"] == "2026-10-01T08:00:00+00:00"
    assert message["id"].startswith("msg_")


def test_extract_messages_from_all_list_newest_first():
    messages = extract_messages(ALL_LIST_STATE)
    assert [m["id"] for m in messages] == ["12345", "12344"]
    assert messages[0]["original_text"].startswith("Følgende opgaver")


def test_extract_messages_handles_missing_or_empty_state():
    assert extract_messages(None) == []
    assert extract_messages({"state": "off"}) == []
    assert extract_messages({"attributes": {"subject": "", "text": "", "sender": ""}}) == []


def test_normalise_message_rejects_non_dict():
    assert normalise_message(None) is None
    assert normalise_message("nope") is None


def test_looks_non_english_detects_danish():
    assert looks_non_english("Følgende opgaver skal afleveres i morgen") is True
    assert looks_non_english("Her er lidt om Signe") is True  # Danish markers
    assert looks_non_english("Vi holder forældremøde") is True


def test_looks_non_english_leaves_english_alone():
    assert looks_non_english("The school trip is on Friday and costs 50 kroner.") is False
    assert looks_non_english("") is False
    assert looks_non_english("12345 !!!") is False
