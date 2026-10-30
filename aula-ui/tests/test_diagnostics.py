"""Exercise the rendered log output, including privacy and concurrent tracing."""

import io
import logging
import os
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import requests

from aula_ui.diagnostics import TRACE, SafeFormatter, correlation, observed, protect
from aula_ui.web import start_server
from test_direct_account import create_controller, install, source_handlers


@pytest.fixture
def output():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(SafeFormatter(
        "%(levelname)s trace=%(correlation)s %(message)s"
    ))
    root = logging.getLogger()
    previous = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield stream
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)


def test_nested_operations_share_trace_without_logging_arguments_or_results(output):
    @observed("inner")
    def inner(private):
        return ["private message", private]

    @observed("outer")
    def outer(private):
        return inner(private)

    assert outer("private credential") == ["private message", "private credential"]
    text = output.getvalue()
    assert "private" not in text
    traces = re.findall(r"trace=([a-f0-9]{12})", text)
    assert len(traces) == 4 and len(set(traces)) == 1
    assert "duration_ms=" in text and "result_count=2" in text
    assert correlation.get() == "-"


def test_exceptions_preserve_behavior_and_log_frames_not_exception_content(output):
    @observed("failure")
    def fail():
        raise ValueError("secret message content")

    with pytest.raises(ValueError, match="secret message"):
        fail()
    text = output.getvalue()
    assert "phase=failed" in text and "error_type=ValueError" in text
    assert "test_diagnostics.py:" in text and ":fail" in text
    assert "secret message" not in text
    assert correlation.get() == "-"


def test_concurrent_operations_have_independent_correlations(output):
    barrier = threading.Barrier(2)

    @observed("concurrent")
    def work():
        barrier.wait(timeout=3)
        return correlation.get()

    with ThreadPoolExecutor(max_workers=2) as executor:
        values = list(executor.map(lambda _: work(), range(2)))
    assert len(set(values)) == 2 and "-" not in values
    assert all(output.getvalue().count("trace=" + value) == 2 for value in values)


def test_secrets_urls_tracebacks_and_log_injection_are_redacted(output):
    secret = "test-secret-credential"
    protect(secret)
    logger = logging.getLogger(__name__)
    logger.info("known=%s Bearer opaque-value access_token=other-value "
                "https://user:pass@example.com/path?code=oauth-private&state=oauth-state "
                "https://example.com/api/aula_calendar/subscription-private.ics "
                "jwt=eyJabc.eyJdef.signature\nforged-line", secret)
    text = output.getvalue()
    for private in (secret, "opaque-value", "other-value", "user:pass",
                    "oauth-private", "oauth-state", "subscription-private", "eyJabc"):
        assert private not in text
    assert text.count("\n") == 1
    assert "\\nforged-line" in text


def test_malformed_url_cannot_break_logging(output):
    logging.getLogger(__name__).warning("bad URL https://[invalid")
    assert "invalid-url-redacted" in output.getvalue()


def test_real_logging_setup_safely_reports_chained_and_unhandled_errors():
    code = """
import logging, threading
from aula_ui.__main__ import _setup_logging
_setup_logging("debug")
try:
    try:
        raise ValueError("private-cause-content")
    except ValueError as error:
        raise RuntimeError("private-outer-content") from error
except RuntimeError:
    logging.getLogger("aula_ui").exception("event=test.chained")
def worker():
    raise ValueError("private-worker-content")
thread = threading.Thread(target=worker)
thread.start()
thread.join()
raise ValueError("private-main-content")
"""
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    result = subprocess.run([sys.executable, "-c", code], env=environment,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 1 and not result.stderr
    assert re.search(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", result.stdout)
    assert "event=test.chained" in result.stdout
    assert "event=thread.unhandled_error" in result.stdout
    assert "event=application.unhandled_error" in result.stdout
    assert "ValueError at" in result.stdout
    assert "RuntimeError at" in result.stdout
    assert "private-" not in result.stdout


@pytest.mark.parametrize("threshold,expected", [
    ("trace", ["TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]),
    ("debug", ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]),
    ("info", ["INFO", "WARNING", "ERROR", "CRITICAL"]),
    ("warning", ["WARNING", "ERROR", "CRITICAL"]),
    ("error", ["ERROR", "CRITICAL"]),
    ("fatal", ["CRITICAL"]),
])
def test_real_logging_severity_thresholds(threshold, expected):
    code = f"""
import logging
from aula_ui.__main__ import _setup_logging
from aula_ui.diagnostics import TRACE
_setup_logging({threshold!r}, color=False)
for level in (TRACE, 10, 20, 30, 40, 50):
    logging.getLogger("aula_ui").log(level, "event=test.level")
"""
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    result = subprocess.run([sys.executable, "-c", code], env=environment,
                            capture_output=True, text=True, timeout=10, check=True)
    assert not result.stderr
    assert re.findall(r"\[(TRACE|DEBUG|INFO|WARNING|ERROR|CRITICAL)\]", result.stdout) == expected
    assert "\033" not in result.stdout


@pytest.mark.parametrize("level,color", [
    (TRACE, "90"), (logging.DEBUG, "36"), (logging.INFO, "32"),
    (logging.WARNING, "33"), (logging.ERROR, "31"), (logging.CRITICAL, "1;31"),
])
def test_severity_color_preserves_plain_label_and_safe_content(level, color):
    record = logging.LogRecord("test", level, __file__, 1, "event=test \033[31m\nforged", (), None)
    formatter = SafeFormatter("[%(levelname)s] %(message)s", color=True)
    rendered = formatter.format(record)
    label = f"[{logging.getLevelName(level)}]"
    assert rendered.startswith(f"\033[{color}m{label}\033[0m")
    assert rendered.count("\033") == 2
    assert "\\x1b[31m\\nforged" in rendered
    assert "\n" not in rendered
    assert record.msg.endswith("\nforged")


def test_no_color_environment_disables_ansi():
    code = """
import logging
from aula_ui.__main__ import _setup_logging
_setup_logging("info")
logging.info("event=test.no_color")
"""
    environment = {**os.environ, "NO_COLOR": "",
                   "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    result = subprocess.run([sys.executable, "-c", code], env=environment,
                            capture_output=True, text=True, timeout=10, check=True)
    assert "[INFO]" in result.stdout and "\033" not in result.stdout


def test_log_options_are_validated(monkeypatch):
    from aula_ui.config import ConfigError, build_config

    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-config-token")
    assert build_config({"log_level": "trace", "log_color": False}).log_color is False
    with pytest.raises(ConfigError, match="log_level"):
        build_config({"log_level": "verbose"})
    with pytest.raises(ConfigError, match="log_color"):
        build_config({"log_color": "false"})


def test_sync_logs_counts_stages_and_planning_without_content(output, tmp_path, monkeypatch):
    controller = create_controller(tmp_path)
    try:
        install(controller)
        source_handlers(controller, monkeypatch)
        controller.poll_cycle()
        text = output.getvalue()
        assert "event=sync.cycle phase=start" in text
        assert "event=source.collect phase=complete" in text
        assert "event=planning.collected" in text
        assert "event=feed.collected item_count=" in text
        assert "event=sync.status status=ok" in text
        assert "Maja message" not in text
        assert "Bring lunch" not in text
        assert "private-access-value" not in text
        assert "private-refresh-value" not in text
    finally:
        controller.close()


def test_ingress_actions_logged_but_bodies_and_query_strings_are_not(output, tmp_path):
    controller = create_controller(tmp_path)
    server = start_server(controller, port=0, allowed_ip="127.0.0.1")
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        response = requests.post(url + "/api/refresh",
                                 headers={"X-Aula-Request": "refresh"}, timeout=5)
        assert response.status_code == 202
        response = requests.post(url + "/api/auth/start",
                                 headers={"X-Aula-Request": "settings"},
                                 json={"username": "private-username", "password": "private-password"}, timeout=5)
        assert response.status_code == 400
        response = requests.get(url + "/unknown-private-path?access_token=private-query", timeout=5)
        assert response.status_code == 404
    finally:
        server.shutdown()
        server.server_close()
        controller.close()
    text = output.getvalue()
    assert "event=ingress.request method=POST route=/api/refresh status=202" in text
    assert "route=/api/auth/start status=400" in text
    assert "route=unknown status=404" in text
    assert not any(private in text for private in
                   ("private-username", "private-password", "private-query", "unknown-private-path"))
