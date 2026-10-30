import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def calendar_reference_date(monkeypatch):
    # Existing source fixtures and event dates are anchored in October 2026.
    monkeypatch.setattr("aula_ui.calendar_feed.calendar_today", lambda *args: date(2026, 10, 1))


@pytest.fixture(autouse=True)
def no_aula_request_spacing(monkeypatch):
    monkeypatch.setattr("aula_ui.direct_source.DirectAulaClient.MIN_INTERVAL", 0)
    # Existing feed tests expect every cycle to refetch; cadence has dedicated tests.
    monkeypatch.setattr("aula_ui.sync_settings.SyncSettings.refresh_seconds", lambda self, kind: 0)
