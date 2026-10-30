"""Feed publishing is coalesced during AI batches; state.json is written compactly."""

import json
import threading
import time

import aula_ui.controller as module
from test_controller import FakeHA, TWO_CHILDREN, build_controller


def counting(controller, monkeypatch):
    builds = []
    real = controller._publish_now

    def build():
        builds.append(controller.store.get("marker"))
        real()
    monkeypatch.setattr(controller, "_publish_now", build)
    return builds


def test_ai_batch_publishes_are_coalesced_with_a_final_publish(tmp_path, monkeypatch):
    controller = build_controller(tmp_path, FakeHA(), children=TWO_CHILDREN)
    builds = counting(controller, monkeypatch)
    with controller.coalesce_publishes():
        for step in range(20):
            controller.store.set("marker", step)
            controller.publish_feed()
    assert builds == [0, 19]
    # Outside a batch (web requests, tests) every call publishes immediately.
    controller.publish_feed()
    controller.publish_feed()
    assert len(builds) == 4


def test_deferred_progress_reaches_the_ui_without_waiting_for_the_batch_end(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "PUBLISH_COALESCE_SECONDS", 0.1)
    controller = build_controller(tmp_path, FakeHA(), children=TWO_CHILDREN)
    builds = counting(controller, monkeypatch)
    with controller.coalesce_publishes():
        controller.store.set("marker", "first")
        controller.publish_feed()
        controller.store.set("marker", "progress")
        controller.publish_feed()
        # A long AI call follows; the trailing timer publishes meanwhile.
        deadline = time.monotonic() + 5
        while builds[-1] != "progress" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert builds == ["first", "progress"]
    # Nothing newer was deferred, so leaving the batch does not rebuild again.
    assert builds == ["first", "progress"]


def test_other_threads_publish_immediately_during_a_batch(tmp_path, monkeypatch):
    controller = build_controller(tmp_path, FakeHA(), children=TWO_CHILDREN)
    builds = counting(controller, monkeypatch)
    with controller.coalesce_publishes():
        controller.publish_feed()
        worker = threading.Thread(target=controller.publish_feed)
        worker.start()
        worker.join()
        assert len(builds) == 2
        controller.publish_feed()
    # The batch's own deferred call is still flushed at the end.
    assert len(builds) == 3


def test_poll_cycle_ends_with_a_current_feed(tmp_path, monkeypatch):
    controller = build_controller(tmp_path, FakeHA(), children=TWO_CHILDREN)
    builds = counting(controller, monkeypatch)

    def update():
        for step in range(5):
            controller.store.set("marker", step)
            controller.publish_feed()
    monkeypatch.setattr(controller.feed, "update", update)
    controller.poll_cycle()
    assert builds and builds[-1] == 4
    assert len(builds) < 5


def test_state_json_is_compact_and_round_trips(tmp_path):
    from aula_ui.state import StateStore
    store = StateStore(str(tmp_path))
    store.set("feed", [{"id": "a", "title": "Æblegrød på fredag"}])
    store.save()
    text = (tmp_path / "state.json").read_text(encoding="utf-8")
    assert "\n" not in text and ", " not in text and '": ' not in text
    assert "Æblegrød" in text
    assert StateStore(str(tmp_path)).get("feed") == [{"id": "a", "title": "Æblegrød på fredag"}]
