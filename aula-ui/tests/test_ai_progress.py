from aula_ui.feed import AI_INTERVAL
from test_content_enrichment import controller_with_thread


def test_progress_counts_summary_title_and_each_message(tmp_path, monkeypatch):
    controller, _ = controller_with_thread(tmp_path, monkeypatch)
    monkeypatch.setattr(controller.feed, "enrich", lambda: None)
    controller.feed.update()
    progress = controller.feed.ai_progress
    assert progress["total"] == 4
    assert progress["completed"] == 0
    assert progress["pending"] == 4
    assert progress["status"] == "processing"
    assert progress["eta_seconds"] is None


def test_completed_progress_and_timing_survive_restart(tmp_path, monkeypatch):
    controller, _ = controller_with_thread(tmp_path, monkeypatch)
    controller.feed.update()
    progress = controller.feed.ai_progress
    assert progress["completed"] == progress["total"] == 4
    assert progress["pending"] == 0
    assert progress["eta_seconds"] == 0
    assert progress["status"] == "idle"
    assert controller.store.get("feed_ai_timings")
    controller.store.save()
    restored, _ = controller_with_thread(tmp_path, monkeypatch)
    assert restored.feed.ai_progress == progress


def test_eta_uses_observed_calls_and_explicit_retry_delay(tmp_path, monkeypatch):
    controller, _ = controller_with_thread(tmp_path, monkeypatch)
    monkeypatch.setattr(controller.feed, "enrich", lambda: None)
    controller.feed.update()
    controller.store.set("feed_ai_timings", [10, 20])
    monkeypatch.setattr("aula_ui.feed.time.monotonic", lambda: 1000)
    assert controller.feed.ai_progress["eta_seconds"] == 60
    controller.feed._retry_at = 1120
    progress = controller.feed.ai_progress
    assert progress["status"] == "retrying"
    assert progress["retry_seconds"] == 120
    assert progress["eta_seconds"] == 180
    item = controller.store.get("feed")[0]
    item["thread_complete"] = False
    assert controller.feed.ai_progress["status"] == "blocked"
    assert controller.feed.ai_progress["eta_seconds"] is None
    controller.provider = None
    assert controller.feed.ai_progress["status"] == "disabled"
    assert controller.feed.ai_progress["eta_seconds"] is None


def test_eta_accounts_for_cached_translation_chunks(tmp_path, monkeypatch):
    controller, _ = controller_with_thread(tmp_path, monkeypatch)
    controller.feed.update()
    controller.store.set("feed_summaries", {})
    controller.store.set("feed_ai_timings", [20])
    progress = controller.feed.ai_progress
    assert progress["completed"] == 3
    assert progress["pending"] == 1
    assert progress["eta_seconds"] == 20


def test_request_timing_excludes_failures_and_bounds_samples(tmp_path, monkeypatch):
    controller, _ = controller_with_thread(tmp_path, monkeypatch)
    monkeypatch.setattr("aula_ui.feed.time.monotonic", lambda: 1000)
    controller.store.set("feed_ai_timings", [10] * 32)
    controller.feed._active_call_start = 980
    controller.feed._ai_call_complete(False)
    assert controller.store.get("feed_ai_timings") == [10] * 32
    controller.feed._active_call_start = 980
    controller.feed._ai_call_complete(True)
    assert controller.store.get("feed_ai_timings") == [10] * 31 + [20]
    controller.store.set("feed_ai_timings", [AI_INTERVAL])
    assert controller.feed.ai_progress["eta_seconds"] == 0
