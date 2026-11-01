"""Synthetic MitID and ingress-prefix checks; never sends a real login request."""

import asyncio
import base64
import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import date, timedelta, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import qrcode
import requests
from playwright.async_api import async_playwright

from aula_ui.account_auth import AuthenticationError, LoginFlow
from aula_ui.avatars import note_author
from aula_ui.web import start_server
from test_direct_account import CHILDREN, TOKENS, create_controller, source_handlers


PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


class AvatarSession:
    class Response:
        status_code, url, headers = 200, "https://media.example/child.png", {"Content-Type": "image/png"}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def iter_content(self, size):
            yield PIXEL_PNG

    def get(self, url, **kwargs):
        return self.Response()


async def responsive(page):
    for theme in ("light", "dark"):
        await page.emulate_media(color_scheme=theme)
        await page.evaluate("window.AulaTheme?.set('system')")
        for width in (320, 390, 768, 1280):
            await page.set_viewport_size({"width": width, "height": 844})
            assert not await page.evaluate("document.documentElement.scrollWidth > innerWidth"), (theme, width)
            assert await page.evaluate("""() =>
              [...document.querySelectorAll('.notification-group .type-option')]
                .filter(label => label.getClientRects().length).every(label => {
                  const box = label.querySelector('input').getBoundingClientRect();
                  const text = label.querySelector('span').getBoundingClientRect();
                  const bounds = label.getBoundingClientRect();
                  return bounds.height >= 44 && text.left >= box.right
                    && Math.abs((box.top + box.height / 2) - (text.top + text.height / 2)) < 2
                    && text.right <= bounds.right;
                })
            """), ("notification tiles align checkbox with label", theme, width)
            assert await page.evaluate("""() => [...document.querySelectorAll('input:not([type=checkbox]):not([type=radio]),select,textarea')]
              .filter(field => field.getClientRects().length)
              .every(field => innerWidth > 800 || parseFloat(getComputedStyle(field).fontSize) >= 16)
            """), ("input focus zoom prevention", theme, width)
            await page.evaluate("document.documentElement.style.setProperty('--safe-area-top', '59px')")
            assert await page.evaluate("""() => {
              const bar = document.querySelector('.topbar');
              if (!bar) return true;
              const controls = bar.querySelector('.top-inner');
              return parseFloat(getComputedStyle(bar).paddingTop) === 59
                && controls.getBoundingClientRect().top - bar.getBoundingClientRect().top >= 59;
            }"""), ("iOS status-bar clearance", theme, width)
            await page.evaluate("document.documentElement.style.removeProperty('--safe-area-top')")
            assert await page.evaluate("""() => {
              const inner = document.querySelector('.top-inner');
              if (!inner) return true;
              const bounds = inner.getBoundingClientRect();
              const items = [...inner.children].filter(child => child.getClientRects().length
                && !child.classList.contains('ai-progress'));
              const rects = items.map(child => child.getBoundingClientRect());
              const row = rects.every(rect => Math.abs(rect.top + rect.height / 2 - (rects[0].top + rects[0].height / 2)) < 4);
              return row && rects.every(rect => rect.left >= bounds.left - 0.5 && rect.right <= bounds.right + 0.5)
                && !inner.querySelector('#whoami, #refresh');
            }"""), ("top bar must fit on one row", theme, width)
            assert await page.evaluate("""() => {
              const controls = document.querySelector('#agenda-controls');
              if (!controls || !controls.getClientRects().length) return true;
              const fields = ['#agenda-date', '#agenda-previous', '#agenda-today', '#agenda-next']
                .map(selector => document.querySelector(selector).getBoundingClientRect());
              const bounds = controls.getBoundingClientRect();
              const inside = fields.every(rect => rect.left >= bounds.left && rect.right <= bounds.right);
              const separate = fields.every((a, i) => fields.slice(i + 1).every(b =>
                a.right <= b.left || b.right <= a.left || a.bottom <= b.top || b.bottom <= a.top));
              return inside && separate && fields.every(rect => rect.height >= 44)
                && (innerWidth > 600 || fields.slice(1).every(rect => rect.top >= fields[0].bottom));
            }"""), ("calendar control overlap or small tap target", theme, width)
            assert await page.evaluate("""() => {
              const luminance = color => {
                const rgb = color.match(/[\\d.]+/g).slice(0, 3).map(Number)
                  .map(value => value / 255).map(value => value <= .04045 ? value / 12.92
                    : ((value + .055) / 1.055) ** 2.4);
                return rgb[0] * .2126 + rgb[1] * .7152 + rgb[2] * .0722;
              };
              const ratio = (a, b) => (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
              const background = luminance(getComputedStyle(document.body).backgroundColor);
              return [...document.querySelectorAll('.post, .calendar-entry')]
                .filter(item => item.getClientRects().length).every(item => {
                  const style = getComputedStyle(item);
                  const card = luminance(style.backgroundColor);
                  return ratio(card, background) >= 1.1
                    && ratio(luminance(style.borderTopColor), card) >= 1.8
                    && ratio(luminance(style.color), card) >= 4.5;
                });
            }"""), ("item contrast", theme, width)
            assert await page.evaluate("""() => {
              const nav = document.querySelector('.bottom-nav [data-tab="calendar"]');
              if (!nav) return true;
              const wide = innerWidth >= 900;
              if (wide === Boolean(nav.getClientRects().length)) return false;
              const side = document.querySelector('#calendar-side');
              const timeline = document.querySelector('#timeline');
              if (!wide || !side.getClientRects().length) return true;
              return !timeline.hidden && side.getBoundingClientRect().right <= timeline.getBoundingClientRect().left;
            }"""), ("wide calendar column beside feed, Calendar tab only on narrow screens", theme, width)
    await page.set_viewport_size({"width": 390, "height": 844})


async def calendar_agenda(page, url):
    today = date.fromisoformat(await page.evaluate(
        "() => `${new Date().getFullYear()}-${String(new Date().getMonth() + 1).padStart(2, '0')}-${String(new Date().getDate()).padStart(2, '0')}`"
    ))
    children = [{"name": "Noah", "slug": "noah"}, {"name": "Alma", "slug": "alma"}]
    items = []

    def item(uid, title, event, child="noah"):
        return {
            "id": f"calendar:{uid}", "title": title, "translated_title": title,
            "content_type": "Calendar", "date": event.get("start") or today.isoformat(),
            "children": [child.title()], "child_slugs": [child], "source": "calendar",
            "calendar_event": event, "summary": "Source summary must stay out of the compact agenda.",
            "original_text": "VERBATIM SOURCE EVIDENCE: a very long message that should only appear in details.",
        }

    items.append(item("native", "Native meeting", {
        "uid": "native", "title": "Native meeting", "start": f"{today.isoformat()}T10:00:00+02:00",
        "end": f"{today.isoformat()}T10:30:00+02:00", "all_day": False,
        "origin": "aula", "review_required": False, "location": "Library",
    }))
    items.append(item("ongoing", "Ongoing camp", {
        "uid": "ongoing", "title": "Ongoing camp",
        "start": (today - timedelta(days=1)).isoformat(),
        "end": (today + timedelta(days=1)).isoformat(), "all_day": True,
        "origin": "ai", "review_required": False,
    }))
    items.append(item("multi", "Multi-day camp", {
        "uid": "multi", "title": "Multi-day camp",
        "start": (today + timedelta(days=2)).isoformat(),
        "end": (today + timedelta(days=4)).isoformat(), "all_day": True,
        "origin": "ai", "review_required": False, "location": "School",
    }, "alma"))
    items.append(item("old", "Earlier event", {
        "uid": "old", "title": "Earlier event",
        "start": (today - timedelta(days=10)).isoformat(), "end": (today - timedelta(days=9)).isoformat(),
        "all_day": True, "origin": "aula", "review_required": False,
    }))
    for index in range(31):
        start = (today + timedelta(days=1)).isoformat()
        items.append(item(f"approved-{index}", f"Approved item {index:02d}", {
            "uid": f"approved-{index}", "title": f"Approved item {index:02d}",
            "start": start, "end": (today + timedelta(days=2)).isoformat(),
            "all_day": True, "origin": "ai", "review_required": False,
        }))
    pending = item("pending", "Needs date check", {
        "uid": "pending", "title": "Needs date check",
        "start": (today + timedelta(days=3)).isoformat(),
        "end": (today + timedelta(days=4)).isoformat(), "all_day": True,
        "origin": "ai", "review_required": True, "issue": "Check the proposed date",
        "recommendation": "The source may refer to next Friday.",
        "source_title": "Original announcement", "source_date": today.isoformat(),
        "source_author": "Teacher", "source_excerpt": "VERBATIM SOURCE EVIDENCE: a very long message that should only appear in details.",
    })
    items.append(pending)
    fixture = {"items": items, "children": children, "errors": [],
               "updated_at": f"{today.isoformat()}T12:00:00Z"}

    async def feed(route):
        await route.fulfill(json=fixture)

    async def profiles(route):
        event_items = [entry["calendar_event"] for entry in fixture["items"]]
        await route.fulfill(json={
            "children": [{"slug": child["slug"], "display_name": child["name"], "institution_type": "school",
                          "timezone": "Europe/Copenhagen", "calendar_enabled": True,
                          "aula_name": child["name"]} for child in children],
            "events": [event for event in event_items if not event.get("review_required")],
            "recommendations": [event for event in event_items if event.get("review_required")],
        })

    edits = []

    async def review(route):
        payload = route.request.post_data_json
        if payload.get("action") == "edit":
            edits.append(payload)
            await route.fulfill(json={"saved": True})
            return
        assert payload == {"uid": "pending", "action": "accept"}
        pending["calendar_event"]["review_required"] = False
        pending["calendar_event"]["origin"] = "ai"
        await route.fulfill(json={"saved": True})

    await page.route("**/api/feed", feed)
    await page.route("**/api/profiles", profiles)
    await page.route("**/api/calendar/review", review)
    await page.goto(url, wait_until="networkidle")
    brand = page.locator(".brand-mark")
    assert await brand.evaluate("image => image.complete && image.naturalWidth === 256")
    assert await page.locator('link[rel="icon"]').get_attribute("href") == "icon.svg"
    await page.locator('[data-tab="calendar"]').click()
    await page.get_by_role("heading", name="Calendar", exact=True).wait_for()
    await page.locator(".calendar-agenda").wait_for()
    assert await page.locator('#timeline a[href="#calendar-settings"]').count() == 0
    assert (await page.locator("#sync").inner_text()).startswith("Updated ")
    assert "items" not in await page.locator("#sync").inner_text()
    assert await page.locator("#sync").evaluate(
        "sync => sync.previousElementSibling.id === 'more'")
    assert await page.locator("#agenda-range").evaluate("""range => {
        const label = range.getBoundingClientRect();
        const picker = document.querySelector('#agenda-date').getBoundingClientRect();
        return label.width > 0 && label.left >= picker.left && label.right <= picker.right + 1
            && label.top >= picker.top && label.bottom <= picker.bottom + 1;
    }""")
    initial_range = await page.locator("#agenda-range").inner_text()
    assert " – " in initial_range
    assert await page.locator(".agenda-count").inner_text() == "34 scheduled events in range"
    assert await page.locator(".calendar-agenda .agenda-day .calendar-entry").count() == 30
    assert await page.get_by_role("button", name="Show more events · 30 of 34", exact=True).count() == 1
    assert await page.locator(".needs-review summary").inner_text() == "Needs review · 1"
    assert "VERBATIM SOURCE EVIDENCE" not in await page.locator(".calendar-agenda").inner_text()
    assert await page.get_by_role("button", name="Accept", exact=True).count() == 0
    assert await page.locator(".calendar-entry-title", has_text="Ongoing camp").count() == 1
    assert "ongoing" in await page.locator(".calendar-entry", has_text="Ongoing camp").inner_text()
    await page.get_by_role("button", name="Show more events · 30 of 34", exact=True).click()
    assert await page.locator(".calendar-agenda .agenda-day .calendar-entry").count() == 34
    multi = page.locator(".calendar-entry", has_text="Multi-day camp")
    assert "through" in await multi.inner_text()
    assert await page.locator(".agenda-day", has_text="Today").count() == 1
    assert await page.locator(".calendar-entry-title", has_text="Native meeting").count() == 1
    native = page.locator(".calendar-entry", has_text="Native meeting")
    assert "Library" in await native.inner_text()
    assert "Noah" in await native.inner_text()
    assert "Aula calendar" in await native.inner_text()
    await page.locator("#agenda-next").click()
    assert await page.locator("#agenda-range").inner_text() != initial_range
    assert await page.locator(".agenda-count").inner_text() == "0 scheduled events in range"
    await page.locator("#agenda-previous").click()
    assert await page.locator("#agenda-range").inner_text() == initial_range
    assert await page.locator(".agenda-count").inner_text() == "34 scheduled events in range"
    await page.locator("#agenda-date").fill((today - timedelta(days=10)).isoformat())
    await page.locator("#agenda-date").press("Enter")
    assert await page.locator(".agenda-count").inner_text() == "1 scheduled event in range"
    assert await page.locator(".calendar-entry-title", has_text="Earlier event").count() == 1
    await page.locator("#agenda-today").click()
    assert await page.locator(".agenda-count").inner_text() == "34 scheduled events in range"
    await page.locator("#search").fill("Approved item 07")
    assert await page.locator(".agenda-count").inner_text() == "1 scheduled event in range"
    assert await page.locator(".calendar-entry-title", has_text="Approved item 07").count() == 1
    await page.locator("#search").fill("")
    await page.locator(".needs-review summary").click()
    await page.locator(".needs-review-entry").click()
    await page.get_by_role("heading", name="Review calendar suggestion", exact=True).wait_for()
    assert "VERBATIM SOURCE EVIDENCE" in await page.locator("#page .calendar-evidence").inner_text()
    await page.get_by_role("button", name="Accept", exact=True).click()
    await page.get_by_role("link", name="← Back", exact=True).click()
    await page.locator(".agenda-count").wait_for()
    assert await page.locator(".agenda-count").inner_text() == "35 scheduled events in range"
    await page.get_by_role("button", name="Show more events · 30 of 35", exact=True).click()
    assert "VERBATIM SOURCE EVIDENCE" not in await page.locator(".calendar-agenda").inner_text()
    assert await page.locator(".calendar-entry-title", has_text="Needs date check").count() == 1
    native_event = items[0]["calendar_event"]
    native_event["calendar_kind"] = "lesson"
    native_event["child_slugs"] = ["noah"]
    await page.reload(wait_until="networkidle")
    await page.locator("#type-filter-summary").click()
    lesson_filter = page.get_by_label("Lessons / classroom bookings", exact=True)
    await lesson_filter.uncheck()
    assert await page.locator(".calendar-entry-title", has_text="Native meeting").count() == 0
    assert await page.locator(".agenda-count").inner_text() == "34 scheduled events in range"
    await page.reload(wait_until="networkidle")
    assert await page.locator(".calendar-entry-title", has_text="Native meeting").count() == 0
    await page.locator("#type-filter-summary").click()
    await page.get_by_label("Lessons / classroom bookings", exact=True).check()
    await page.locator(".calendar-entry", has_text="Native meeting").click()
    await page.get_by_role("heading", name="Calendar event details", exact=True).wait_for()
    starts = page.get_by_label("Starts", exact=True)
    assert await starts.get_attribute("type") == "datetime-local"
    expected = await page.evaluate("""value => new Intl.DateTimeFormat('sv-SE', {
      timeZone: 'Europe/Copenhagen', year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hourCycle: 'h23'
    }).format(new Date(value)).replace(' ', 'T')""", native_event["start"])
    assert (await starts.input_value())[:16] == expected
    await responsive(page)
    await page.get_by_role("button", name="Save changes", exact=True).click()
    await page.locator(".calendar-agenda").wait_for()
    assert edits[-1]["changes"]["start"] == native_event["start"]
    await page.locator(".calendar-entry", has_text="Native meeting").click()
    await page.get_by_label("All-day event", exact=True).check()
    first = page.get_by_label("First day", exact=True)
    last = page.get_by_label("Last day (optional)", exact=True)
    assert await first.get_attribute("type") == "date"
    await first.fill(today.isoformat())
    await last.fill((today + timedelta(days=2)).isoformat())
    await page.get_by_role("button", name="Save changes", exact=True).click()
    await page.locator(".calendar-agenda").wait_for()
    assert edits[-1]["changes"]["start"] == today.isoformat()
    assert edits[-1]["changes"]["end"] == (today + timedelta(days=3)).isoformat()
    assert edits[-1]["changes"]["all_day"] is True
    await responsive(page)
    await wide_calendar(page, url)


async def wide_calendar(page, url):
    await page.goto(url, wait_until="networkidle")
    await page.set_viewport_size({"width": 1280, "height": 900})
    side = page.locator("#calendar-side")
    await side.locator(".calendar-agenda").wait_for()
    assert not await page.locator('[data-tab="calendar"]').is_visible()
    assert await page.locator("#feed-title").inner_text() == "Feed"
    assert await side.locator("#agenda-controls").is_visible()
    assert await side.locator('a[href="#calendar-settings"]').count() == 0
    assert (await side.locator("#side-sync").inner_text()).startswith("Updated ")
    assert await side.locator("#side-sync").evaluate(
        "sync => sync.previousElementSibling.id === 'side-more'")
    assert await page.locator("#sync").evaluate(
        "sync => sync.nextElementSibling.id === 'feed'")
    assert await page.locator("#feed .calendar-entry").count() == 0
    count = lambda: side.locator(".agenda-count").inner_text()
    before = int((await count()).split()[0])
    assert before > 0 and await side.locator(".calendar-entry-title", has_text="Native meeting").count() == 1
    await side.locator("#side-filter-summary").click()
    await side.get_by_label("Lessons / classroom bookings", exact=True).uncheck()
    assert int((await count()).split()[0]) == before - 1
    await side.get_by_label("Lessons / classroom bookings", exact=True).check()
    assert int((await count()).split()[0]) == before
    await page.locator("#agenda-next").click()
    assert await count() == "0 scheduled events in range"
    await page.locator("#agenda-today").click()
    for width, height in ((900, 1180), (1024, 768), (1366, 1024)):
        await page.set_viewport_size({"width": width, "height": height})
        await page.evaluate("() => window.scrollTo(0, 0)")
        geometry = await page.evaluate("""() => {
          const panel = document.querySelector('#calendar-side').getBoundingClientRect();
          const nav = document.querySelector('.bottom-nav').getBoundingClientRect();
          const controls = document.querySelector('#agenda-controls').getBoundingClientRect();
          const field = document.querySelector('#agenda-date').getBoundingClientRect();
          const side = document.querySelector('#calendar-side');
          const limit = parseFloat(getComputedStyle(side).maxHeight);
          const fits = side.scrollHeight <= side.clientHeight;
          const feed = document.querySelector('#timeline').getBoundingClientRect();
          return {bottomGap: fits ? 16 : innerHeight - panel.bottom, limitGap: innerHeight - (panel.top + limit), navClear: nav.left >= panel.right,
                  panelLeft: panel.right <= feed.left,
                  fieldInside: field.left >= controls.left && field.right <= controls.right + 0.5};
        }""")
        assert 0 <= geometry["bottomGap"] <= 24 and 0 <= geometry["limitGap"] <= 24, (width, geometry)
        assert geometry["navClear"] and geometry["fieldInside"] and geometry["panelLeft"], (width, geometry)
    await page.evaluate("() => window.scrollTo(0, 0)")
    await page.set_viewport_size({"width": 1280, "height": 900})
    focus_date = await page.locator("#agenda-date").input_value()
    assert await side.is_visible()
    assert await page.locator("body").evaluate("body => body.classList.contains('split-calendar')")
    feed_width = await page.locator("#timeline").evaluate("feed => feed.getBoundingClientRect().width")
    await page.get_by_role("button", name="Hide calendar sidebar", exact=True).click()
    assert await side.is_hidden()
    toggle = page.get_by_role("button", name="Show calendar", exact=True)
    assert await toggle.get_attribute("aria-expanded") == "false"
    assert await toggle.evaluate("button => document.activeElement === button")
    assert await page.locator("body").evaluate("body => body.classList.contains('calendar-collapsed')")
    wider_feed = await page.locator("#timeline").evaluate("feed => feed.getBoundingClientRect().width")
    assert wider_feed > feed_width, (feed_width, wider_feed,
                                    await page.locator("body").get_attribute("class"))
    await page.reload(wait_until="networkidle")
    assert await side.is_hidden()
    await page.get_by_role("button", name="Show calendar", exact=True).click()
    assert await side.is_visible()
    assert await page.get_by_role("button", name="Hide calendar", exact=True).get_attribute("aria-expanded") == "true"
    assert await page.locator("#agenda-date").input_value() == focus_date
    await page.get_by_role("button", name="Hide calendar", exact=True).click()
    await page.set_viewport_size({"width": 390, "height": 844})
    await page.locator('[data-tab="calendar"]').click()
    assert await page.locator("#timeline #agenda-controls").is_visible()
    assert await page.locator("#calendar-toggle").is_hidden()
    await page.set_viewport_size({"width": 1280, "height": 900})
    assert await side.is_hidden()
    await page.get_by_role("button", name="Show calendar", exact=True).click()
    await side.locator(".calendar-entry", has_text="Native meeting").click()
    await page.get_by_role("heading", name="Calendar event details", exact=True).wait_for()
    assert await side.is_hidden()
    await page.go_back()
    await side.locator(".calendar-agenda").wait_for()
    await page.goto(url + "calendar-view", wait_until="networkidle")
    await side.locator(".calendar-agenda").wait_for()
    assert await page.locator("#feed-title").inner_text() == "Feed"
    await page.set_viewport_size({"width": 390, "height": 844})
    await page.wait_for_function("() => document.querySelector('#feed-title').textContent === 'Calendar'")
    assert await side.is_hidden()
    assert await page.locator("#timeline #agenda-controls").is_visible()
    assert await page.locator('[data-tab="calendar"]').is_visible()


async def chronological_agenda(page, url):
    today = date.fromisoformat(await page.evaluate(
        "() => `${new Date().getFullYear()}-${String(new Date().getMonth() + 1).padStart(2, '0')}-${String(new Date().getDate()).padStart(2, '0')}`"
    ))
    items = [{
        "id": f"calendar:order-{offset}", "title": f"Ordered event {offset}",
        "content_type": "Calendar", "date": (today + timedelta(days=offset)).isoformat(),
        "children": [], "child_slugs": [],
        "calendar_event": {
            "uid": f"order-{offset}", "title": f"Ordered event {offset}",
            "start": (today + timedelta(days=offset)).isoformat(),
            "end": (today + timedelta(days=offset + 1)).isoformat(),
            "all_day": True, "origin": "aula", "review_required": False,
        },
    } for offset in (2, 5, -4)]
    await page.route("**/api/feed", lambda route: route.fulfill(json={"items": items, "children": []}))
    await page.goto(url + "#calendar", wait_until="networkidle")
    await page.reload(wait_until="networkidle")
    await page.locator("#agenda-today").click()
    assert await page.locator(".calendar-entry-title").all_text_contents() == [
        "Ordered event -4", "Ordered event 2", "Ordered event 5",
    ]
    await responsive(page)


async def presentation(page, url):
    summary = (
        "## Latest update\n\nThe **trip is confirmed**. Please reply by Friday.\n\n"
        "- Bring **lunch**\n- Wear comfortable shoes\n\n"
        "1. Read the update\n2. Confirm attendance\n\n"
        'Key activities: <ul><li>Playground construction</li><li>Indoor <strong>dance</strong></li></ul>'
        '<p><em>Bring boots tomorrow.</em></p><ol><li>Pack lunch</li><li>Bring water</li></ol>\n\n'
        '<img src=x onerror="window.summaryInjected=true">\n'
        '<script>window.summaryInjected=true</script>'
    )
    messages = [
        {"id": "failed", "sender": "Office", "date": "2026-10-25T10:20:00Z",
         "original_text": "Fejlet besked", "translation_error": "Service temporarily unavailable"},
        {"id": "pending", "sender": "Office", "date": "2026-10-25T10:15:00Z",
         "original_text": "Afventer oversættelse", "translation_pending": True},
        {"id": "no-provider", "sender": "School office", "date": "2026-10-25T10:12:00Z",
         "original_text": "The office closes at four.", "is_translated": None, "translation_status": "original"},
        {"id": "translated", "sender": "Teacher", "date": "2026-10-25T10:10:00Z",
         "original_text": "Tak for svaret.", "translated_text": "Thank you for replying.", "is_translated": True},
        {"id": "english-reply", "sender": "Parent", "date": "2026-10-25T10:05:00Z",
         "original_text": "We will bring lunch.", "is_translated": False},
        {"id": "english", "sender": "Parent", "date": "2026-10-25T10:00:00Z",
         "original_text": "We can attend.", "translated_text": "We can attend.", "is_translated": False},
    ]
    item = {
        "id": "chat-fixture", "title": "Original thread title", "translated_title": "School trip",
        "content_type": "Message", "date": "2026-10-25T10:20:00Z", "summary": summary,
        "original_text": "Untouched source.", "sender": "Teacher", "children": [], "child_slugs": [],
        "thread_messages": messages, "unread": True,
    }
    progress = {"completed": 10, "total": 50, "pending": 40, "status": "processing",
                "eta_seconds": 600, "retry_seconds": 0}
    await page.route("**/api/feed", lambda route: route.fulfill(json={
        "items": [item], "children": [], "ai_progress": progress,
    }))
    await page.goto(url, wait_until="networkidle")
    assert await page.locator("#ai-progress strong").inner_text() == "Pending tasks 10/50"
    assert await page.locator("#ai-progress span").inner_text() == "~10 min left"
    assert await page.locator("#feed .rich-text h3").inner_text() == "Latest update"
    assert await page.locator("#feed .rich-text strong").all_text_contents() == ["trip is confirmed", "lunch", "dance"]
    assert await page.locator("#feed .rich-text em").inner_text() == "Bring boots tomorrow."
    assert await page.locator("#feed .rich-text ul li").count() == 4
    assert await page.locator("#feed .rich-text ol li").count() == 4
    assert "<ul>" not in await page.locator("#feed .rich-text").inner_text()
    assert await page.locator("#feed .rich-text img, #feed .rich-text script").count() == 0
    await page.locator("#search").fill("School trip")
    assert await page.get_by_role("button", name="Clear search").is_visible()
    await page.get_by_role("button", name="Clear search").click()
    assert await page.locator("#search").input_value() == ""
    assert await page.get_by_role("button", name="Clear search").is_hidden()
    assert await page.locator(".sort-label").count() == 0
    await page.get_by_role("button", name="Read", exact=True).click()
    await page.get_by_role("heading", name="Conversation", exact=True).wait_for()
    assert await page.get_by_role("heading", name="Original source", exact=True).count() == 0
    assert await page.get_by_text("Latest messages first · Original messages are available below each reply.", exact=True).count() == 0
    assert await page.locator("#page .rich-text strong").count() == 3
    assert await page.locator(".thread-message").evaluate_all(
        "nodes => nodes.map(node => node.dataset.messageId)"
    ) == ["failed", "pending", "no-provider", "translated", "english-reply", "english"]
    assert await page.locator(".thread-message.consecutive").count() == 2
    assert await page.locator(".chat-avatar").count() == len(messages)
    assert await page.locator(".chat-language").all_text_contents() == [
        "Original message", "English translation", "English original", "English original",
    ]
    no_provider = await page.locator('[data-message-id="no-provider"]').inner_text()
    assert "The office closes at four." in no_provider
    assert "Translation unavailable" not in no_provider
    assert await page.locator('[data-message-id="english-reply"] .thread-translation').inner_text() == "We will bring lunch."
    assert "Translation pending" in await page.locator('[data-message-id="pending"]').inner_text()
    assert "Service temporarily unavailable" in await page.locator('[data-message-id="failed"]').inner_text()
    translated = page.locator('[data-message-id="translated"]')
    await translated.get_by_text("Show original message", exact=True).click()
    assert await translated.locator(".original").inner_text() == "Tak for svaret."
    assert await page.locator(".thread-message").get_by_text("Me", exact=True).count() == 0
    assert await page.evaluate("window.summaryInjected") is None
    assert '<img src=x onerror="window.summaryInjected=true">' in await page.locator("#page .rich-text.body-text").inner_text()
    await responsive(page)
    progress.update(status="retrying", eta_seconds=None, retry_seconds=120)
    await page.evaluate("load()")
    assert await page.locator("#ai-progress span").inner_text() == "Retry in ~2 min"
    assert await page.locator(".conversation-more").is_hidden()
    item["thread_messages"] = [
        {"id": f"message-{index}", "sender": "School", "date": f"2026-10-25T10:{index:02d}:00Z",
         "original_text": f"English message {index}", "is_translated": False}
        for index in range(25)
    ]
    await page.reload(wait_until="networkidle")
    assert await page.locator(".thread-message").count() == 10
    assert await page.locator(".thread-message").first.get_attribute("data-message-id") == "message-24"
    await page.get_by_role("button", name="Show 10 more messages", exact=True).click()
    assert await page.locator(".thread-message").count() == 20
    # A feed refresh must not collapse already loaded replies.
    await page.evaluate("load()")
    assert await page.locator(".thread-message").count() == 20
    await page.get_by_role("button", name="Show 5 more messages", exact=True).click()
    assert await page.locator(".thread-message").count() == 25
    assert await page.locator(".thread-message").last.get_attribute("data-message-id") == "message-0"
    assert await page.locator(".conversation-more").is_hidden()
    await responsive(page)
    long_text = " ".join(
        f"Sentence {index} preserves every detail about the children's day."
        for index in range(20)
    )
    item["thread_messages"] = [{
        "id": "long-message", "sender": "Teacher", "date": "2026-10-25T10:00:00Z",
        "original_text": long_text, "translated_text": long_text, "is_translated": False,
    }]
    await page.reload(wait_until="networkidle")
    content = page.locator('[data-message-id="long-message"] .thread-translation')
    assert await content.locator("p").count() >= 3
    assert " ".join((await content.inner_text()).split()) == long_text
    await responsive(page)


async def check_pdf_download(page, link, controller, pdf):
    stored = controller.store.get("feed", [])
    controller.store.set("feed", [{"id": "post:42"}])
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr("aula_ui.web._call", lambda *args: {"data": {"attachments": [
                {"name": "Guide.pdf", "file": {"url": "https://media-prod.aula.dk/file"}}
                for _ in range(4)
            ]}})
            patch.setattr("aula_ui.web.download_attachment", lambda attachment: (pdf, "application/pdf"))
            async with page.expect_download() as downloaded:
                await link.click()
            download = await downloaded.value
            assert download.suggested_filename == "Guide.pdf"
            assert Path(await download.path()).read_bytes() == pdf
    finally:
        controller.store.set("feed", stored)


async def installed_ios_download(browser, url):
    page = await browser.new_page(viewport={"width": 390, "height": 844}, user_agent=(
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    ))
    await page.add_init_script("""
        const media = window.matchMedia.bind(window);
        window.matchMedia = query => query === "(display-mode: standalone)"
            ? {matches: true, media: query, addEventListener() {}, removeEventListener() {}} : media(query);
        window.__shared = [];
        navigator.canShare = data => Array.isArray(data?.files);
        navigator.share = async data => {
            window.__shared.push({name: data.files[0].name, type: data.files[0].type, size: data.files[0].size});
        };
    """)
    item = {
        "id": "post:42", "title": "Bike ride", "content_type": "Post",
        "date": "2026-10-23T13:25:00Z", "summary": "Text-only summary.",
        "original_text": "Bakkeskoven bicycle trip", "children": [], "child_slugs": [],
        "attachments": [{"name": "Guide.pdf", "kind": "file", "url": "https://media-prod.aula.dk/guide"}],
    }
    await page.route("**/api/feed", lambda route: route.fulfill(json={"items": [item], "children": []}))
    await page.route("**/api/feed/media?*", lambda route: route.fulfill(json={"attachments": item["attachments"]}))
    await page.route("**/api/feed/attachment?*", lambda route: route.fulfill(
        body=b"%PDF-1.4 fixture", content_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="Guide.pdf"'},
    ))
    try:
        await page.goto(url + "#post/post%3A42", wait_until="networkidle")
        before = page.url
        await page.get_by_role("link", name="Download Guide.pdf", exact=True).click()
        await page.wait_for_function("() => window.__shared.length === 1")
        assert await page.evaluate("window.__shared") == [
            {"name": "Guide.pdf", "type": "application/pdf", "size": 16}]
        assert page.url == before
        assert await page.locator(".attachment-file-action").inner_text() == "Download"
        await page.get_by_role("link", name="Download Guide.pdf", exact=True).click()
        await page.wait_for_function("() => window.__shared.length === 2")
        assert page.url == before
    finally:
        await page.close()


async def sync_settings_page(browser, url):
    import tempfile
    from aula_ui.sync_settings import SyncSettings
    page = await browser.new_page(viewport={"width": 390, "height": 844})
    scratch = tempfile.TemporaryDirectory(prefix="aula-sync-settings-")
    model = SyncSettings(scratch.name, 300)
    writes = []

    async def api(route):
        if route.request.method == "POST":
            assert route.request.headers["x-aula-request"] == "settings"
            writes.append(route.request.post_data_json)
            try:
                model.update(route.request.post_data_json)
            except ValueError as error:
                await route.fulfill(status=400, json={"error": str(error)})
                return
        await route.fulfill(json=model.snapshot())

    await page.route("**/api/feed", lambda route: route.fulfill(json={"items": [], "children": []}))
    await page.route("**/api/sync-settings", api)
    try:
        await page.goto(url + "#profiles", wait_until="networkidle")
        await page.get_by_role("link", name="Sync ›").click()
        full = page.locator("input[name=full_sync_minutes]")
        await full.wait_for()
        assert await full.input_value() == "5", "legacy poll_interval migrated"
        assert writes == []
        assert "Aula requests per hour" in await page.locator(".sync-estimate").inner_text()
        await full.fill("20")
        await page.get_by_label("Messages until", exact=True).select_option("22")
        await page.get_by_role("button", name="Save sync settings").click()
        await page.get_by_text("Saved. A sync starts now.", exact=True).wait_for()
        assert writes[-1]["full_sync_minutes"] == 20 and writes[-1]["messages_to_hour"] == 22
        assert model.values()["full_sync_minutes"] == 20
        await page.get_by_label("Messages from", exact=True).select_option("23")
        await page.get_by_label("Messages until", exact=True).select_option("22")
        await page.get_by_role("button", name="Save sync settings").click()
        await page.get_by_role("alert").wait_for()
        assert model.values()["messages_to_hour"] == 22
        await page.get_by_role("button", name="Restore defaults").click()
        await page.get_by_text("Saved. A sync starts now.", exact=True).wait_for()
        assert model.values() == model.snapshot()["defaults"]
        assert await page.locator("input[name=full_sync_minutes]").input_value() == "15"
        width = await page.evaluate("document.documentElement.scrollWidth")
        assert width <= 390, width
    finally:
        await page.close()
        scratch.cleanup()


async def retired_siri_settings(browser, url):
    page = await browser.new_page(viewport={"width": 390, "height": 844})
    try:
        await page.goto(url + "#profiles", wait_until="networkidle")
        await page.get_by_role("heading", name="Settings", exact=True).wait_for()
        assert await page.get_by_text("Siri & Shortcuts", exact=True).count() == 0
        await page.goto(url + "#siri-settings", wait_until="networkidle")
        await page.get_by_role("heading", name="Page not found", exact=True).wait_for()
    finally:
        await page.close()


async def mcp_settings(browser, url):
    page = await browser.new_page(viewport={"width": 390, "height": 844})
    key = "k" * 43
    settings = {
        "children": [], "calendar_path": "/api/aula_calendar/fixture.ics", "calendar_url": "",
        "mcp_enabled": True, "mcp_path": f"/mcp/{key}",
        "mcp_url": f"https://aula.example/mcp/{key}", "mcp_allow_writes": True,
        "calendar_count": 0, "calendar_categories": [], "calendar_error": "",
        "events": [], "archived_events": [], "recommendations": [],
        "declined_recommendations": [], "extraction_available": True, "recognition_warnings": [],
    }

    async def profiles(route):
        await route.fulfill(json=settings)

    await page.route("**/api/profiles", profiles)
    try:
        await page.goto(url + "#profiles", wait_until="networkidle")
        await page.locator('a[href="#mcp-settings"]').click()
        await page.get_by_label("MCP connector URL").wait_for()
        assert await page.get_by_label("MCP connector URL").input_value() == settings["mcp_url"]
        assert await page.get_by_role("button", name="Copy MCP URL").count() == 1
        assert await page.get_by_text("MCP write tools are enabled", exact=False).is_visible()
        assert await page.get_by_text("Siri & Shortcuts", exact=True).count() == 0
    finally:
        await page.close()


async def sickness_reporting(browser, url):
    page = await browser.new_page(viewport={"width": 390, "height": 844})
    writes = []
    reads = []
    now = datetime.now(timezone.utc)
    date_key = now.date().isoformat()
    result = {
        "date": date_key, "valid_until": (now + timedelta(hours=12)).isoformat(),
        "notice": "This informs daycare/SFO staff. Notify the school teacher separately.",
        "children": [
            {"slug": "maja", "name": "Maja", "can_report": True, "sick": False, "reason": ""},
            {"slug": "emil", "name": "Emil", "can_report": False, "sick": False,
             "reason": "Not permitted by Aula."},
        ],
    }

    async def sickness_api(route):
        if route.request.method == "POST":
            writes.append(route.request.post_data_json)
            assert route.request.headers["x-aula-request"] == "settings"
            result["children"][0]["sick"] = writes[-1]["sick"]
        else:
            reads.append(True)
        await route.fulfill(json=result)

    await page.route("**/api/feed", lambda route: route.fulfill(json={
        "items": [], "children": [{"slug": "maja", "name": "Maja"}, {"slug": "emil", "name": "Emil"}],
    }))
    await page.route("**/api/sickness", sickness_api)
    try:
        await page.goto(url + "#calendar", wait_until="networkidle")
        controls = page.locator("#presence")
        status = page.locator("#sickness-status")
        report = controls.get_by_role("button", name="Maja: Sick today", exact=True)
        await report.wait_for()
        await page.wait_for_function("() => !document.querySelector('.sickness-toggle').disabled")
        assert writes == [] and len(reads) == 1
        assert await report.get_attribute("aria-pressed") == "false"
        assert await report.locator(".sickness-box").inner_text() == ""
        assert await controls.get_by_role("button", name="Emil: Sick today").is_disabled()
        assert await report.locator("button").count() == 0
        assert await page.locator("#sickness-main, #sickness-side, .sickness-controls").count() == 0
        assert await report.evaluate("node => node.closest('.presence-child').querySelector('strong').textContent") == "Maja"
        assert await report.evaluate("node => node.getBoundingClientRect().height") <= 40
        selected = await controls.locator(".presence-filter").first.get_attribute("aria-pressed")
        page.once("dialog", lambda dialog: dialog.dismiss())
        await report.click()
        assert writes == [] and await report.get_attribute("aria-pressed") == "false"
        page.once("dialog", lambda dialog: dialog.accept())
        await report.click()
        await page.wait_for_function("() => document.querySelector('.sickness-toggle').getAttribute('aria-pressed') === 'true'")
        assert writes == [{"child": "maja", "date": date_key, "sick": True}]
        assert await report.is_enabled() and await report.locator(".sickness-box").inner_text() == "✓"
        assert await status.get_by_text("Maja is reported sick today. Added to your calendar.").is_visible()
        assert await controls.locator(".presence-filter").first.get_attribute("aria-pressed") == selected
        page.once("dialog", lambda dialog: dialog.dismiss())
        await report.click()
        assert len(writes) == 1 and await report.get_attribute("aria-pressed") == "true"
        page.once("dialog", lambda dialog: dialog.accept())
        await report.click()
        await page.wait_for_function("() => document.querySelector('.sickness-toggle').getAttribute('aria-pressed') === 'false'")
        assert writes[-1] == {"child": "maja", "date": date_key, "sick": False}
        assert await status.get_by_text("Removed today's sick-day calendar entry.", exact=False).is_visible()
        # A failed write must not optimistically check the toggle.
        await page.route("**/api/sickness", lambda route: route.fulfill(status=502, json={"error": "Aula unavailable"}))
        page.once("dialog", lambda dialog: dialog.accept())
        await report.click()
        await status.get_by_text("Change not confirmed:", exact=False).wait_for()
        assert await report.get_attribute("aria-pressed") == "false" and await report.is_disabled()
        await page.unroute("**/api/sickness")
        await page.route("**/api/sickness", sickness_api)
        result["valid_until"] = (now - timedelta(seconds=1)).isoformat()
        await status.get_by_role("button", name="Check Aula status").click()
        await page.wait_for_function("() => document.querySelector('.sickness-toggle').disabled")
        assert await report.get_attribute("aria-pressed") == "false"
        assert len(writes) == 2
        result["valid_until"] = (now + timedelta(hours=12)).isoformat()
        await page.set_viewport_size({"width": 1200, "height": 900})
        await page.goto(url, wait_until="networkidle")
        await page.get_by_role("button", name="Maja: Sick today").wait_for()
        assert await page.locator(".sickness-toggle").count() == 2
    finally:
        await page.close()


async def album_tagging(browser, url):
    page = await browser.new_page(viewport={"width": 390, "height": 844})
    item = {
        "id": "album:77", "title": "Cykel tur", "content_type": "Album",
        "date": "2026-10-26T20:27:00Z", "summary": "Bicycle trip.", "original_text": "Tur",
        "children": [], "child_slugs": [], "album": {"id": "77", "thumbs": 0},
    }
    photos = [{"id": str(n), "type": "image", "name": f"photo{n}.jpg",
               "can_edit_tags": True, "tagged_children": []} for n in (1, 2)]
    writes = []
    fail = False

    async def write(route):
        value = route.request.post_data_json
        writes.append(value)
        assert route.request.headers["x-aula-request"] == "settings"
        if fail:
            await route.fulfill(status=502, json={"error": "Aula rejected this tag"})
        else:
            photo = next(photo for photo in photos if photo["id"] == value["media"])
            photo["tagged_children"] = ["maja"] if value["tagged"] else []
            await route.fulfill(json=photo)

    await page.route("**/api/feed", lambda route: route.fulfill(json={"items": [item], "children": []}))
    await page.route("**/api/gallery/album?*", lambda route: route.fulfill(json={
        "id": "77", "count": 2, "media": photos, "tag_choices": [{"slug": "maja", "name": "Maja"}],
    }))
    await page.route("**/api/gallery/image?*", lambda route:
                     route.fulfill(status=404, body="") if "size=original" in route.request.url
                     else route.fulfill(body=PIXEL_PNG, content_type="image/png"))
    await page.route("**/api/gallery/tag", write)
    try:
        photos[1]["tagged_children"] = ["maja"]
        await page.goto(url + "#post/album%3A77", wait_until="networkidle")
        save = page.get_by_role("button", name="Save 1 tagged photo", exact=True)
        await save.wait_for()
        assert await page.get_by_text("Save all", exact=False).count() == 0
        assert await page.locator(".album-tag-bar, .album-select, .album-tag").count() == 0
        assert await page.locator(".album-tag-badge").count() == 1
        assert writes == []
        await page.get_by_role("button", name="View photo1.jpg", exact=True).click()
        viewer = page.get_by_role("dialog")
        await viewer.get_by_role("button", name="Tag Maja", exact=True).click()
        await viewer.get_by_text("Tagged in Aula.", exact=True).wait_for()
        assert writes == [{"album": "77", "media": "1", "child": "maja", "tagged": True}]
        await page.keyboard.press("Escape")
        await page.get_by_role("button", name="Save 2 tagged photos", exact=True).wait_for()
        assert await page.locator(".album-tag-badge").count() == 2
        fail = True
        await page.get_by_role("button", name="View photo2.jpg", exact=True).click()
        await viewer.get_by_role("button", name="Remove tag for Maja", exact=True).click()
        await viewer.get_by_text("Tag not confirmed: Aula rejected this tag", exact=True).wait_for()
        await viewer.get_by_role("button", name="Remove tag for Maja", exact=True).wait_for()
        await page.keyboard.press("Escape")
        await page.get_by_role("button", name="Save 2 tagged photos", exact=True).wait_for()
        fail = False
        await page.get_by_role("button", name="View photo1.jpg", exact=True).click()
        viewer = page.get_by_role("dialog")
        tag = viewer.get_by_role("button", name="Remove tag for Maja", exact=True)
        await tag.wait_for()
        assert await tag.get_attribute("aria-pressed") == "true"
        geometry = await page.evaluate("""() => {
            const box = selector => document.querySelector(selector).getBoundingClientRect();
            return {close: box('.image-viewer-toolbar .image-viewer-close'), nav: box('.image-viewer-actions'),
                    share: box('.image-viewer-share'), tag: box('.image-viewer-tag'),
                    content: box('.image-viewer-content'), width: innerWidth, height: innerHeight};
        }""")
        assert geometry["close"]["right"] > geometry["width"] - 30 and geometry["close"]["top"] < 60
        assert geometry["close"]["height"] <= 40 and geometry["share"]["height"] <= 40
        nav_mid = geometry["nav"]["left"] + geometry["nav"]["width"] / 2
        assert abs(nav_mid - geometry["width"] / 2) < 4
        assert geometry["share"]["top"] > geometry["content"]["bottom"] - 1
        assert geometry["tag"]["top"] > geometry["content"]["bottom"] - 1
        await tag.click()
        await viewer.get_by_role("button", name="Tag Maja", exact=True).wait_for()
        await viewer.get_by_text("Tag removed in Aula.", exact=True).wait_for()
        assert writes[-1] == {"album": "77", "media": "1", "child": "maja", "tagged": False}
        await page.keyboard.press("ArrowRight")
        await viewer.get_by_role("button", name="Remove tag for Maja", exact=True).wait_for()
        await page.keyboard.press("Escape")
        assert await page.locator(".album-tag-badge").count() == 1
        originals = []
        page.on("request", lambda request: originals.append(request.url)
                if "size=original" in request.url else None)
        await page.get_by_role("button", name="Save 1 tagged photo", exact=True).click()
        await page.get_by_text("Could not save: Photo unavailable", exact=True).wait_for()
        assert len(originals) == 1 and "media=2&" in originals[0]
        photos[1]["tagged_children"] = []
        for photo in photos:
            photo["can_edit_tags"] = False
        await page.reload(wait_until="networkidle")
        await page.get_by_text("Aula does not allow your account to edit tags in this album.").wait_for()
        assert await page.get_by_role("button", name="No tagged photos yet", exact=True).is_disabled()
        await page.get_by_role("button", name="View photo1.jpg", exact=True).click()
        await page.get_by_role("dialog").get_by_role("button", name="Close", exact=True).wait_for()
        assert await page.locator(".image-viewer-tag").count() == 0
    finally:
        await page.close()


async def image_recovery(browser, url):
    page = await browser.new_page(viewport={"width": 390, "height": 844})
    item = {"id": "album:77", "title": "Photo recovery", "content_type": "Album",
            "date": "2026-11-01", "summary": "Photos", "children": [], "child_slugs": [],
            "album": {"id": "77", "thumbs": 0}}
    photos = [{"id": str(n), "type": "image", "name": f"Photo {n}",
               "can_edit_tags": False, "tagged_children": []} for n in (1, 2, 3)]
    calls = []
    recover = False

    async def image_request(route):
        from urllib.parse import urlsplit, parse_qs
        query = parse_qs(urlsplit(route.request.url).query)
        calls.append(query)
        photo, size = query["media"][0], query["size"][0]
        good = (photo == "1" and query.get("retry") == ["1"]) or (
            photo == "2" and size == "medium") or (photo == "3" and recover)
        await route.fulfill(body=PIXEL_PNG if good else b"Broken image",
                            content_type="image/png")

    await page.route("**/api/feed", lambda route: route.fulfill(json={"items": [item], "children": []}))
    await page.route("**/api/gallery/album?*", lambda route: route.fulfill(json={
        "id": "77", "count": 3, "media": photos, "tag_choices": [],
    }))
    await page.route("**/api/gallery/image?*", image_request)
    try:
        await page.goto(url + "#post/album%3A77", wait_until="networkidle")
        await page.get_by_role("button", name="Retry Photo 3", exact=True).wait_for()
        tiles = page.locator(".album-photo-tile")
        assert await tiles.count() == 3
        assert await tiles.nth(0).locator("img").evaluate("image => image.naturalWidth > 0")
        assert await tiles.nth(1).locator("img").evaluate("image => image.naturalWidth > 0 && image.src.includes('size=medium')")
        assert await tiles.nth(2).locator("img").evaluate("image => image.style.visibility === 'hidden'")
        assert len([call for call in calls if call["media"] == ["3"]]) == 3
        recover = True
        await page.get_by_role("button", name="Retry Photo 3", exact=True).click()
        await page.wait_for_function("() => document.querySelectorAll('.album-photo-tile img')[2].naturalWidth > 0")
        assert await page.locator(".image-unavailable").count() == 0
        await page.get_by_role("button", name="View Photo 1", exact=True).click()
        viewer = page.get_by_role("dialog")
        await page.wait_for_function("() => document.querySelector('.image-viewer-content img').naturalWidth > 0")
        assert any(call["media"] == ["1"] and call["size"] == ["large"] and call.get("retry") == ["1"]
                   for call in calls)
        await page.keyboard.press("ArrowRight")
        await viewer.get_by_role("button", name="Retry Photo 2", exact=True).wait_for()
        await page.keyboard.press("ArrowRight")
        await page.wait_for_function("() => document.querySelector('.image-viewer-content img').naturalWidth > 0")
        assert await viewer.locator(".image-unavailable").count() == 0
        await page.keyboard.press("Escape")
    finally:
        await page.close()


async def mark_read_on_open(browser, url):
    page = await browser.new_page(viewport={"width": 390, "height": 844})
    item = {
        "id": "thread:42", "title": "LICE!", "content_type": "Message", "unread": True,
        "date": "2026-10-26T20:27:00Z", "summary": "Check for lice.", "original_text": "Lus",
        "children": [], "child_slugs": [], "thread_messages": [],
    }
    posts = []
    await page.route("**/api/feed", lambda route: route.fulfill(json={"items": [item], "children": []}))
    async def read(route):
        posts.append(route.request.post_data_json)
        await route.fulfill(json={"read": True, "changed": True})
    await page.route("**/api/feed/read", read)
    try:
        await page.goto(url, wait_until="networkidle")
        assert await page.locator("#feed .unread-dot").count() == 1
        await page.wait_for_timeout(300)
        assert posts == []
        await page.goto(url + "#thread/thread%3A42", wait_until="networkidle")
        await page.wait_for_function("() => !document.querySelector('#page .unread-dot')")
        assert posts == [{"id": "thread:42"}]
        await page.evaluate("route()")
        await page.wait_for_timeout(300)
        assert posts == [{"id": "thread:42"}]
    finally:
        await page.close()


async def notification_history(browser, url):
    page = await browser.new_page(viewport={"width": 390, "height": 844})
    entries = [
        {"id": "a", "item_id": "post:1", "category": "post", "title": "Trip", "message": "Boots",
         "sent_at": "2026-10-26T10:00:00+00:00", "read": False, "available": True},
        {"id": "b", "item_id": "", "category": "nap", "title": "Alma woke up", "message": "Slept",
         "sent_at": "2026-10-26T09:00:00+00:00", "read": True, "available": False},
    ]
    state = {"unread": 1}
    posts = []
    def body():
        return {"items": [{**e, "read": e["read"] or not state["unread"]} for e in entries], "unread": state["unread"]}
    await page.route("**/api/feed", lambda route: route.fulfill(json={"items": [
        {"id": "post:1", "title": "Trip", "content_type": "Post", "date": "2026-10-26T10:00:00Z",
         "summary": "Boots", "children": [], "child_slugs": []}], "children": []}))
    await page.route("**/api/notifications/history", lambda route: route.fulfill(json=body()))
    async def read(route):
        posts.append(route.request.post_data_json)
        state["unread"] = 0
        await route.fulfill(json=body())
    await page.route("**/api/notifications/read", read)
    try:
        await page.goto(url, wait_until="networkidle")
        assert await page.locator(".bell-dot:visible").count() == 1
        await page.locator(".bell-link").click()
        await page.wait_for_selector(".history-row")
        assert await page.locator(".history-row").count() == 2
        assert await page.locator(".history-row.unread").count() == 1
        await page.locator(".history-row.unread a").click()
        await page.wait_for_function("() => document.querySelector('.bell-dot').hidden")
        assert posts == [{"id": "post:1"}]
    finally:
        await page.close()


async def post_photo_grid(browser, url):
    page = await browser.new_page(viewport={"width": 390, "height": 844})
    photos = [{"name": f"Photo {index}.png", "kind": "image",
               "url": f"https://media.example.test/grid-{index}.png"} for index in range(3)]
    item = {"id": "post:grid", "title": "Post photos", "content_type": "Post",
            "date": "2026-10-23T13:25:00Z", "summary": "Photos from today.",
            "children": [], "child_slugs": [], "attachments": photos}
    await page.add_init_script("""
        Object.defineProperty(navigator, "platform", {get: () => "MacIntel"});
        Object.defineProperty(navigator, "maxTouchPoints", {get: () => 2});
        window.__photoBatches = [];
        navigator.canShare = data => Array.isArray(data?.files);
        navigator.share = async data => window.__photoBatches.push(
            data.files.map(file => ({name: file.name, type: file.type})));
    """)
    await page.route("**/api/feed", lambda route: route.fulfill(json={"items": [item], "children": []}))
    await page.route("**/api/feed/media?*", lambda route: route.fulfill(json={"attachments": photos}))
    await page.route("https://media.example.test/grid-*.png",
                     lambda route: route.fulfill(body=PIXEL_PNG, content_type="image/png"))
    requests_seen = []

    async def photo_file(route):
        requests_seen.append(route.request.url)
        await route.fulfill(body=PIXEL_PNG, content_type="image/png")

    await page.route("**/api/feed/attachment?*", photo_file)
    try:
        await page.goto(url + "#post/post%3Agrid", wait_until="networkidle")
        grid = page.locator(".detail-media .album-grid")
        await grid.wait_for()
        assert await grid.locator(".album-photo").count() == 3
        assert await page.locator(".detail-media .album-tags, .detail-media .album-tag").count() == 0
        assert await grid.locator("figcaption").count() == 0
        for width in (320, 390, 768, 1280):
            await page.set_viewport_size({"width": width, "height": 844})
            assert await grid.locator("img").evaluate_all("""images => images.every(image => {
                const rect = image.getBoundingClientRect();
                return Math.abs(rect.width - rect.height) < 1 && rect.right <= innerWidth;
            })""")
        await page.get_by_role("button", name="Save all 3 photos", exact=True).click()
        await page.get_by_role("button", name="Tap to open share sheet", exact=True).click()
        await page.wait_for_function("() => window.__photoBatches.length === 1")
        assert await page.evaluate("window.__photoBatches[0]") == [
            {"name": photo["name"], "type": "image/png"} for photo in photos]
        previews = [request for request in requests_seen if "preview=1" in request]
        originals = [request for request in requests_seen if "preview=1" not in request]
        assert len(previews) == len(originals) == 3
        assert await grid.locator("img").evaluate_all(
            "images => images.every(image => new URL(image.src).origin === location.origin && image.src.includes('preview=1'))"
        )
        for index, request in enumerate(originals):
            assert "id=post%3Agrid" in request and f"index={index}" in request
        await grid.get_by_role("button", name="View Photo 1.png full width", exact=True).click()
        await page.get_by_role("dialog", name="Photo 1.png", exact=True).wait_for()
        assert await page.get_by_role("button", name="Share Photo 1.png", exact=True).count() == 1
    finally:
        await page.close()


async def media_details(page, url, controller):
    pdf = await page.pdf(page_ranges="1")
    await page.add_init_script("""
        window.__imageShares = [];
        navigator.canShare = data => Array.isArray(data?.files);
        navigator.share = async data => {
          window.__imageShares.push(data.files
            ? {title: data.title, name: data.files[0].name, type: data.files[0].type,
               size: data.files[0].size}
            : {title: data.title, url: data.url});
        };
    """)
    item = {
        "id": "post:42", "title": "Bike ride", "content_type": "Post",
        "date": "2026-10-23T13:25:00Z", "summary": "Text-only summary.",
        "original_text": "Bakkeskoven bicycle trip", "children": [], "child_slugs": [],
        "attachments": [
            {"name": "Invitation", "kind": "image", "url": "https://media.example.test/photo.png"},
            {"name": "Guide.pdf", "kind": "file", "url": "https://media-prod.aula.dk/guide"},
        ],
        "recognized_events": [{
            "uid": "bike-review", "source_id": "post:42", "title": "Bike ride",
            "source_title": "Bike ride", "source_excerpt": "Bakkeskoven bicycle trip",
            "start": "", "end": "", "review_required": True, "origin": "ai",
            "issue": "Date missing", "recommendation": "Check the attached invitation.",
        }],
    }
    calendar_item = {
        "id": "calendar:bike-review", "title": "Bike ride", "content_type": "Calendar",
        "date": "2026-10-23", "children": [], "child_slugs": [],
        "calendar_event": item["recognized_events"][0],
    }
    await page.route("**/api/feed", lambda route: route.fulfill(json={
        "items": [item, calendar_item], "children": [],
    }))
    async def accept(route):
        assert route.request.post_data_json == {"uid": "bike-review", "action": "accept"}
        item["recognized_events"][0]["review_required"] = False
        await route.fulfill(json={"saved": True})
    await page.route("**/api/calendar/review", accept)
    await page.route("**/api/feed/media?*", lambda route: route.fulfill(json={
        "attachments": [
            {"name": "Invitation", "kind": "image", "url": "https://media.example.test/photo.png"},
            {"name": "Missing photo", "kind": "image", "url": "https://media.example.test/missing.png"},
            {"name": "No link", "kind": "image", "url": ""},
            {"name": "Guide.pdf", "kind": "file", "url": "https://media-prod.aula.dk/guide"},
            {"name": "Notes.docx", "kind": "file", "url": "https://media-prod.aula.dk/notes"},
        ],
    }))
    await page.route("https://media.example.test/photo.png", lambda route: route.fulfill(
        path=Path(__file__).resolve().parents[1] / "icon.png", content_type="image/png",
        headers={"Access-Control-Allow-Origin": "*"},
    ))
    await page.route("https://media.example.test/missing.png", lambda route: route.fulfill(status=404))
    async def file(route):
        assert "id=post%3A42" in route.request.url
        if "download=1" in route.request.url:
            await route.continue_()
            return
        if "index=0" in route.request.url:
            await route.fulfill(path=Path(__file__).resolve().parents[1] / "icon.png",
                                content_type="image/png")
            return
        if "index=3" in route.request.url:
            await route.fulfill(body=pdf, content_type="application/pdf", headers={
                "Content-Disposition": 'attachment; filename="Guide.pdf"'
                if "download=1" in route.request.url else 'inline; filename="Guide.pdf"',
            })
        else:
            await route.fulfill(body=b"Office fixture", content_type="application/octet-stream")
    await page.route("**/api/feed/attachment?*", file)
    await page.goto(url, wait_until="networkidle")
    assert await page.locator("#feed .content-indicators").get_by_text("1 image", exact=True).count() == 1
    assert await page.locator("#feed .content-indicators").get_by_text("1 attachment", exact=True).count() == 1
    assert await page.locator("#feed .content-indicators").get_by_text("1 pending event", exact=True).count() == 1
    await page.goto(url + "#post/post%3A42", wait_until="networkidle")
    await page.get_by_role("heading", name="Related calendar events", exact=True).wait_for()
    assert await page.locator(".related-calendar").get_by_role("button", name="Accept", exact=True).count() == 1
    assert await page.locator(".related-calendar .notice").count() == 0
    await page.locator(".related-calendar").get_by_text("Review details", exact=True).click()
    assert "Check the attached invitation" in await page.locator(".related-calendar").inner_text()
    await page.get_by_text("Image unavailable here. Open in Aula.", exact=True).wait_for()
    assert await page.locator(".detail-media .album-photo img").evaluate_all(
        "images => images.filter(image => image.complete && image.naturalWidth > 0).length === 1"
    )
    assert "Image unavailable here" in await page.locator(".detail-media").inner_text()
    assert "no displayable image link" in await page.locator(".detail-media").inner_text()
    assert await page.locator(".detail-media").get_by_role("heading", name="Attachments", exact=True).count() == 1
    await check_pdf_download(page, page.get_by_role("link", name="Download Guide.pdf", exact=True),
                             controller, pdf)
    assert await page.get_by_role("button", name="Open Guide.pdf", exact=True).count() == 0
    assert await page.locator(".detail-media").get_by_text("Download", exact=True).count() == 2
    assert await page.locator(".attachment-file-type").all_inner_texts() == ["PDF", "DOCX"]
    for width in (320, 390, 768, 1280):
        await page.set_viewport_size({"width": width, "height": 844})
        assert await page.locator(".attachment-file-download").evaluate_all(
            "links => links.every(link => link.getBoundingClientRect().right <= innerWidth && link.getBoundingClientRect().height >= 44)"
        )
    await page.set_viewport_size({"width": 390, "height": 844})
    assert await page.get_by_role("link", name="Download Notes.docx", exact=True).get_attribute("download") == "Notes.docx"
    assert "Images are not read by the AI" not in await page.locator(".detail-media").inner_text()
    await page.get_by_role("button", name="View Invitation full width", exact=True).click()
    viewer = page.get_by_role("dialog", name="Invitation", exact=True)
    await viewer.wait_for()
    assert await viewer.locator("img").evaluate("image => image.getBoundingClientRect().width >= innerWidth - 20")
    await viewer.get_by_role("button", name="Share Invitation", exact=True).click()
    await page.wait_for_function("() => window.__imageShares.length === 1")
    shared = await page.evaluate("window.__imageShares[0]")
    assert shared["title"] == "Invitation.png" and shared["name"] == "Invitation.png"
    assert shared["type"] == "image/png" and shared["size"] > 0
    await viewer.get_by_text("Image shared.", exact=True).wait_for()
    await page.evaluate("navigator.canShare = () => false")
    await viewer.get_by_role("button", name="Share Invitation", exact=True).click()
    await page.wait_for_function("() => window.__imageShares.length === 2")
    assert await page.evaluate("window.__imageShares[1].url") == "https://media.example.test/photo.png"
    await page.keyboard.press("Escape")
    await page.wait_for_function("() => !document.querySelector('.image-viewer')")
    assert await page.locator(".image-viewer").count() == 0
    await page.get_by_role("button", name="View Invitation full width", exact=True).click()
    await page.evaluate("document.documentElement.style.setProperty('--safe-area-top', '59px')")
    await page.get_by_role("dialog").locator("img").evaluate("image => image.style.minHeight = '2000px'")
    await page.locator(".image-viewer-content").evaluate("content => content.scrollTop = 1000")
    close = page.get_by_role("dialog").get_by_role("button", name="Close", exact=True)
    assert await close.evaluate("""button => {
        const rect = button.getBoundingClientRect();
        return rect.top >= 59 && rect.bottom <= innerHeight && rect.height >= 32 &&
            button.contains(document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2));
    }""")
    await page.get_by_role("dialog").get_by_role("button", name="Close", exact=True).click()
    await page.wait_for_function("() => !document.querySelector('.image-viewer')")
    assert await page.locator(".image-viewer").count() == 0
    await page.evaluate("document.documentElement.style.removeProperty('--safe-area-top')")
    await responsive(page)
    await page.goto(url, wait_until="networkidle")
    attention = page.get_by_role("link", name="1 requires attention", exact=True)
    await attention.click()
    await page.get_by_role("heading", name="Requires attention", exact=True).wait_for()
    assert await page.locator("#page .event-list > li").count() == 1
    assert await page.locator("#page").get_by_role("button", name="Accept", exact=True).count() == 1
    assert await page.locator("#unread-count").count() == 0
    await responsive(page)
    await page.locator("#page").get_by_role("button", name="Accept", exact=True).click()
    await page.get_by_text("Nothing needs your attention.", exact=True).wait_for()
    assert await page.locator("#attention-count").is_hidden()
    await page.unroute("**/api/feed/attachment?*", file)


async def push_settings(page, controller):
    """Enable, test and turn off app (Web Push) notifications with a stubbed browser push service."""
    from test_webpush import browser_keys
    _, p256dh, auth = browser_keys()
    pushed = []

    class PushSession:
        def post(self, url, **kwargs):
            pushed.append((url, kwargs))
            return type("Response", (), {"status_code": 201})()

    controller.notifier.push.session = PushSession()
    await page.evaluate("""([p256dh, auth]) => {
        let current = null;
        const sub = {
            endpoint: "https://fcm.googleapis.com/fcm/send/browser-test",
            options: {applicationServerKey: null},
            toJSON() { return {endpoint: this.endpoint, keys: {p256dh, auth}}; },
            async unsubscribe() { current = null; return true; },
        };
        Notification.requestPermission = async () => "granted";
        PushManager.prototype.getSubscription = async () => current;
        PushManager.prototype.subscribe = async () => { current = sub; return sub; };
    }""", [p256dh, auth])
    await page.evaluate("location.hash = '#profiles'")
    await page.get_by_role("link", name="Notifications ›", exact=True).click()
    form = page.locator(".push-form")
    await form.get_by_label("Device name").fill("Browser test")
    await page.locator(".push-categories input[value=post]").uncheck()
    await page.locator(".push-categories input[value=message]").check()
    await form.get_by_role("button", name="Turn on for this device").click()
    await page.locator(".push-status", has_text="Turned on").wait_for()
    devices = controller.notifier.push.all()
    assert [(d["name"], "message" in d["categories"], "post" in d["categories"]) for d in devices] == [
        ("Browser test", True, False)], devices
    await form.get_by_role("button", name="Send test").click()
    await page.locator(".push-status", has_text="Test sent").wait_for()
    assert pushed and pushed[-1][0] == "https://fcm.googleapis.com/fcm/send/browser-test"
    assert pushed[-1][1]["headers"]["Content-Encoding"] == "aes128gcm"
    await page.locator(".push-categories input[value=homework]").check()
    await page.locator(".push-categories").get_by_label("Naps", exact=True).check()
    await form.get_by_role("button", name="Save for this device").click()
    await page.locator(".push-status", has_text="Saved for this device").wait_for()
    assert "homework" in controller.notifier.push.all()[0]["categories"]
    await page.locator(".push-form").get_by_role("button", name="Turn off").click()
    await page.locator(".push-status", has_text="Turned off").wait_for()
    assert controller.notifier.push.all() == []
    assert await page.get_by_role("button", name="Turn on for this device").count() == 1


async def list_back_position(page, url):
    items = [{
        "id": f"return-{index}", "title": f"Return item {index}",
        "content_type": "Message", "date": datetime.now(timezone.utc).isoformat(),
        "summary": "Keep my list position. " * 10,
        "child_slugs": ["noah" if index % 2 else "alma"], "children": ["Noah" if index % 2 else "Alma"],
        "thread_messages": [{"id": f"reply-{index}", "sender": "Teacher",
                             "original_text": "Recorded message", "is_translated": False}],
    } for index in range(65)]
    fixture = {"items": items, "children": [{"name": "Noah", "slug": "noah"}, {"name": "Alma", "slug": "alma"}],
               "presence": [], "errors": []}
    async def feed(route):
        await route.fulfill(json=fixture)
    await page.route("**/api/feed", feed)
    await page.goto(url, wait_until="networkidle")
    await page.locator("#presence .presence-filter", has_text="Noah").click()
    assert await page.locator(".presence-filter[aria-pressed=true]").count() == 1
    assert await page.locator("#feed .post").evaluate_all(
        "nodes => nodes.every(node => Number(node.dataset.id.split('-')[1]) % 2 === 0)")
    await page.locator("#presence .presence-filter", has_text="Alma").click()
    assert await page.locator(".presence-filter[aria-pressed=true]").count() == 0
    assert await page.locator("#feed .post").count() == 0
    await page.locator("#presence .presence-filter", has_text="Noah").click()
    assert await page.locator("#feed .post").evaluate_all(
        "nodes => nodes.every(node => Number(node.dataset.id.split('-')[1]) % 2 === 1)")
    await page.locator("#presence .presence-filter", has_text="Alma").click()
    assert await page.locator(".presence-filter[aria-pressed=true]").count() == 2
    await page.locator("#search").fill("Return item")
    assert await page.locator("#feed .post").count() == 20
    await page.locator("#more").click()
    assert await page.locator("#feed .post").count() == 40
    await page.locator("#more").click()
    assert await page.locator("#feed .post").count() == 60
    target = page.locator('#feed .post[data-id="return-44"]')
    await target.scroll_into_view_if_needed()
    y = await page.evaluate("scrollY")
    assert y > 1000
    await target.get_by_role("button", name="Read", exact=True).click()
    await page.get_by_role("heading", name="Conversation", exact=True).wait_for()
    await page.locator("#page > .page-back").click()
    await page.locator("#timeline").wait_for(state="visible")
    await page.wait_for_function("expected => Math.abs(scrollY - expected) <= 2", arg=y)
    assert await page.locator("#feed .post").count() == 60
    assert await page.locator("#search").input_value() == "Return item"
    assert await page.locator('.presence-filter[aria-pressed=true]').count() == 2
    await target.get_by_role("button", name="Read", exact=True).click()
    await page.get_by_role("heading", name="Conversation", exact=True).wait_for()
    await page.go_back()
    await page.locator("#timeline").wait_for(state="visible")
    await page.wait_for_function("expected => Math.abs(scrollY - expected) <= 2", arg=y)
    assert await page.locator("#feed .post").count() == 60
    await page.get_by_role("button", name="Clear search").click()
    await page.unroute("**/api/feed", feed)


async def related_reading(page, url):
    item = {"id": "reading-fixture", "content_type": "Message", "title": "Lus",
            "summary": "Check for head lice", "child_slugs": [], "children": [],
            "thread_messages": [{"id": "message", "original_text": "Der er lus i klassen"}]}
    routine = {"id": "reading-event", "content_type": "Calendar", "title": "Skovtur",
               "summary": "Walk in the forest", "child_slugs": [], "children": []}
    from aula_ui.related_links import LICE
    posts, gets, waits = [], [], []
    state = {"status": "idle", "links": []}
    async def feed(route):
        await route.fulfill(json={"items": [item, routine], "children": []})
    async def reading(route):
        if route.request.method == "POST":
            body = json.loads(route.request.post_data)
            posts.append(body)
            state.update(status="pending", links=[])
            await route.fulfill(json=state)
            return
        if "wait=1" in route.request.url:
            waits.append(route.request.url)
            await asyncio.sleep(0.3)
            links = LICE if len(posts) == 1 else LICE[:1]
            state.update(status="ready", links=links, checked_at="2026-10-27T12:00:00+00:00")
        else:
            gets.append(route.request.url)
        await route.fulfill(json=state)
    await page.route("**/api/feed", feed)
    await page.route("**/api/feed/reading?*", reading)
    await page.route("**/api/feed/reading", reading)
    await page.goto(url, wait_until="networkidle")
    await page.locator('#feed .post[data-id="reading-fixture"]').get_by_role("button", name="Read", exact=True).click()
    lookup = page.get_by_role("button", name="Look up additional information", exact=True)
    await lookup.wait_for()
    await page.wait_for_timeout(500)
    assert posts == [] and await page.locator(".reading-link").count() == 0
    await lookup.click()
    await page.locator(".reading-link").first.wait_for()
    assert posts == [{"id": "reading-fixture"}] and len(waits) == 1
    assert await page.locator(".reading-link").count() == 3
    assert await page.locator(".reading-link").evaluate_all(
        "links => links.every(link => link.target === '_blank' && link.rel.includes('noreferrer') && link.href.startsWith('https://'))")
    regenerate = page.get_by_role("button", name="Regenerate links", exact=True)
    await regenerate.wait_for()
    # Links stay; periodic feed refreshes re-render the page without polling the lookup again.
    gets_before = len(gets)
    await page.evaluate("load()")
    await page.evaluate("load()")
    await page.wait_for_timeout(3500)
    assert len(gets) == gets_before and len(waits) == 1 and len(posts) == 1
    assert await page.locator(".reading-link").count() == 3
    await responsive(page)
    await regenerate.click()
    await page.wait_for_function("() => document.querySelectorAll('.reading-link').length === 1")
    assert posts[-1] == {"id": "reading-fixture", "regenerate": True} and len(waits) == 2
    await page.get_by_role("button", name="Regenerate links", exact=True).wait_for()
    # Any item type can request links.
    state.update(status="idle", links=[])
    await page.goto(url + "#post/reading-event", wait_until="networkidle")
    await page.get_by_role("button", name="Look up additional information", exact=True).wait_for()
    await page.unroute("**/api/feed/reading?*", reading)
    await page.unroute("**/api/feed/reading", reading)
    async def failed_reading(route):
        if route.request.method == "POST":
            posts.append(True)
        await route.fulfill(json={"status": "error", "links": [],
                                  "error": "Web search failed. No automatic retry will be made."})
    await page.route("**/api/feed/reading?*", failed_reading)
    await page.route("**/api/feed/reading", failed_reading)
    await page.goto(url + "#post/reading-fixture", wait_until="networkidle")
    await page.reload(wait_until="networkidle")
    await page.get_by_role("button", name="Regenerate links", exact=True).wait_for()
    assert "No automatic retry" in await page.locator(".related-reading").inner_text()
    count = len(posts)
    await page.wait_for_timeout(3500)
    assert len(posts) == count
    await page.unroute("**/api/feed/reading?*", failed_reading)
    await page.unroute("**/api/feed/reading", failed_reading)
    await page.unroute("**/api/feed", feed)


async def presence_strip(page, url):
    now = datetime.now(timezone.utc)
    fixture = {
        "items": [
            {"id": "presence:noah", "content_type": "Presence", "title": "Legacy presence",
             "child_slugs": ["noah"]},
        ],
        "children": [{"name": "Noah", "slug": "noah"}, {"name": "Alma", "slug": "alma"}],
        "presence": [
            {"child_slug": "noah", "name": "Noah", "status": "Present", "state": "present",
             "check_in_time": "08:10", "check_out_time": "", "timezone": "Europe/Copenhagen",
             "valid_until": (now + timedelta(minutes=15)).isoformat(), "updated_at": now.isoformat()},
            {"child_slug": "alma", "name": "Alma", "status": "Picked up", "state": "away",
             "check_in_time": "08:30", "check_out_time": "16:00", "timezone": "Europe/Copenhagen",
             "valid_until": (now - timedelta(minutes=1)).isoformat(), "updated_at": now.isoformat()},
        ],
        "errors": [], "updated_at": now.isoformat(),
    }
    async def feed(route):
        await route.fulfill(json=fixture)
    await page.route("**/api/feed", feed)
    await page.goto(url, wait_until="networkidle")
    await page.locator("#presence").wait_for(state="visible")
    assert await page.locator(".presence-child").count() == 2
    text = await page.locator("#presence").inner_text()
    assert "Present" in text and "Arrived 08:10" in text and "Unknown" in text
    assert "Picked up" not in text and "16:00" not in text
    assert await page.locator(".post").count() == 0
    assert await page.locator("#type-filters").get_by_text("Presence", exact=True).count() == 0
    await responsive(page)
    await page.locator("#timeline .type-filters").evaluate("element => element.open = true")
    await page.locator("#presence .presence-filter", has_text="Alma").click()
    assert await page.locator(".presence-child").count() == 2
    assert await page.locator(".presence-filter[aria-pressed=true]").count() == 1
    assert await page.locator("#filters").count() == 0
    assert "Alma" in await page.locator("#presence").inner_text()
    await page.locator('[data-tab="saved"]').click()
    await page.locator("#presence").wait_for(state="visible")
    assert await page.locator(".presence-child").count() == 2
    await page.locator('[data-tab="home"]').click()
    await page.locator("#presence").wait_for(state="visible")
    nap_start = now - timedelta(hours=2)
    nap_end = now - timedelta(minutes=40)
    alma = fixture["presence"][1]
    alma.update({
        "status": "Napping", "state": "present", "check_out_time": "",
        "valid_until": (now + timedelta(minutes=15)).isoformat(),
        "sleep_intervals": [{"start": nap_start.isoformat(), "end": None}],
    })
    await page.reload(wait_until="networkidle")
    await page.locator("#presence").get_by_text("Napping", exact=True).wait_for()
    text = await page.locator("#presence").inner_text()
    assert "Napping since" in text and "Last checked" not in text and "Not yet checked" not in text
    alma["status"] = "Present"
    alma["sleep_intervals"][0]["end"] = nap_end.isoformat()
    await page.reload(wait_until="networkidle")
    await page.locator("#presence").get_by_text("Slept 1h 20m", exact=False).wait_for()
    assert "Slept 1h 20m" in await page.locator("#presence").inner_text()
    await responsive(page)
    await page.unroute("**/api/feed", feed)


async def update_toast(page, url):
    from aula_ui import __version__

    deployed = {"version": __version__}

    async def version(route):
        await route.fulfill(json=deployed, headers={"Cache-Control": "no-store"})

    await page.route("**/api/version", version)
    await page.goto(url + "#profiles", wait_until="networkidle")
    await page.get_by_role("heading", name="Settings", exact=True).wait_for()
    assert (await page.locator(".app-version").inner_text()) == f"Aula UI version {__version__}"
    assert await page.locator("#update-toast:not([hidden])").count() == 0
    deployed["version"] = "99.0.0"
    await page.evaluate("window.AulaVersion.check()")
    toast = page.locator("#update-toast")
    await toast.get_by_text("A new version (99.0.0) is available.").wait_for()
    reload = toast.get_by_role("button", name="Reload", exact=True)
    box = await reload.bounding_box()
    assert box["height"] >= 36 and box["y"] + box["height"] <= (page.viewport_size or {"height": 844})["height"]
    await toast.get_by_role("button", name="Dismiss update notice").click()
    assert await toast.is_hidden()
    await page.evaluate("window.AulaVersion.check()")
    assert await toast.is_hidden()
    deployed["version"] = "99.0.1"
    await page.evaluate("window.AulaVersion.check()")
    await toast.get_by_text("A new version (99.0.1) is available.").wait_for()
    async with page.expect_navigation(timeout=8000):
        await toast.get_by_role("button", name="Reload", exact=True).click()
    await page.get_by_role("heading", name="Settings", exact=True).wait_for()
    await page.unroute("**/api/version", version)


async def post_collapse(page, url):
    now = datetime.now(timezone.utc).isoformat()
    fixture = {
        "items": [
            {"id": "post:collapse-a", "content_type": "Post", "title": "Collapse me",
             "summary": "Hidden when collapsed", "child_slugs": ["noah"], "children": ["Noah"], "date": now},
            {"id": "post:collapse-b", "content_type": "Post", "title": "Stay open",
             "summary": "Always visible body", "child_slugs": ["noah"], "children": ["Noah"], "date": now},
        ],
        "children": [{"name": "Noah", "slug": "noah"}], "errors": [], "updated_at": now,
    }
    async def feed(route):
        await route.fulfill(json=fixture)
    await page.route("**/api/feed", feed)
    await page.evaluate("() => localStorage.removeItem('aula-collapsed-posts')")
    await page.goto(url, wait_until="networkidle")
    first = page.locator('.post[data-id="post:collapse-a"]')
    second = page.locator('.post[data-id="post:collapse-b"]')
    await first.wait_for()
    start = page.url
    await first.locator(".meta").click()
    assert "collapsed" in await first.get_attribute("class")
    assert page.url == start
    assert await first.locator(".collapse-toggle").get_attribute("aria-expanded") == "false"
    assert await first.get_by_text("Hidden when collapsed").is_hidden()
    assert await first.locator(".post-actions").is_hidden()
    assert await first.get_by_role("heading", name="Collapse me").is_visible()
    assert await second.get_by_text("Always visible body").is_visible()
    await responsive(page)
    await page.reload(wait_until="networkidle")
    await first.wait_for()
    assert "collapsed" in await first.get_attribute("class")
    assert "collapsed" not in await second.get_attribute("class")
    await first.locator(".collapse-toggle").focus()
    await page.keyboard.press("Enter")
    assert "collapsed" not in await first.get_attribute("class")
    assert await first.get_by_text("Hidden when collapsed").is_visible()
    await first.locator(".collapse-toggle").focus()
    await page.keyboard.press("Space")
    assert "collapsed" in await first.get_attribute("class")
    await first.locator(".post-menu").click()
    await page.wait_for_function("() => location.hash.startsWith('#post/') || location.pathname.includes('/post/')")
    await page.goto(url, wait_until="networkidle")
    await first.wait_for()
    await first.locator(".collapse-toggle").click()
    assert json.loads(await page.evaluate("() => localStorage.getItem('aula-collapsed-posts')")) == []
    await page.unroute("**/api/feed", feed)


async def verify():
    # System temp, never the source tree: an interrupted run must not leave data dirs behind.
    with tempfile.TemporaryDirectory(
        prefix="aula-addon-login-browser-"
    ) as temporary, pytest.MonkeyPatch.context() as patch:
        controller = create_controller(Path(temporary))
        source_handlers(controller, patch)
        patch.setattr("aula_ui.account.DirectAulaClient.discover", lambda self: CHILDREN)
        approved = threading.Event()
        mode = {"fail": False}
        ai_settings = {
            "llm_provider": "ha_ai_task", "applies": True, "ai_task_entities": [],
            "default_label": "Home Assistant default",
            "entities": [
                {"entity_id": "ai_task.google_ai_task", "name": "Google AI Task",
                 "state": "2026-10-26T00:00:00+00:00", "available": True},
                {"entity_id": "ai_task.openai_ai_task", "name": "OpenAI AI Task",
                 "state": "unknown", "available": True},
                {"entity_id": "ai_task.broken", "name": "Broken AI",
                 "state": "unavailable", "available": False},
            ],
            "selected_available": None, "error": "",
            "fallback": {"configured": True, "provider": "anthropic", "model": "claude-sonnet-4-5"},
            "active_provider": "ha_ai_task", "last_provider": None, "providers": [],
            "sources": [
                {"id": "anthropic", "label": "Anthropic (my API key)", "configured": False, "model": "m"},
                {"id": "google", "label": "Google Gemini (my API key)", "configured": True, "model": "gemini"},
                {"id": "cloudflare", "label": "Cloudflare Workers AI", "configured": True, "model": "llama"},
            ],
        }
        ai_settings_posts = []

        async def ai_settings_route(route):
            if route.request.method == "GET":
                await route.fulfill(json=ai_settings)
                return
            payload = route.request.post_data_json
            assert route.request.headers.get("x-aula-request") == "settings"
            assert route.request.headers.get("content-type", "").startswith("application/json")
            ai_settings_posts.append(payload)
            if "ai_credential" in payload:
                for source in ai_settings["sources"]:
                    if source["id"] == payload["ai_credential"]["source"]:
                        source.update(configured=True, stored=True)
            else:
                ai_settings["providers"] = payload["ai_providers"]
            await route.fulfill(json={**ai_settings, "saved": True})

        qr = qrcode.QRCode()
        qr.add_data("synthetic approval only")
        qr.make()

        class MitID:
            status_message = "Scan the animated QR and approve"

            def get_current_qr_codes(self):
                return (qr, qr)

        class Client:
            def __init__(self, **kwargs):
                self.session = requests.Session()

            def get_mitid_client(self):
                return MitID()

            def authenticate(self):
                if mode["fail"]:
                    raise RuntimeError("raw authentication material must never appear")
                assert self.identity_selector(["Guardian identity", "Employee identity"]) == 1
                while not approved.wait(0.05):
                    if self.session.cancelled.is_set():
                        raise AuthenticationError("Authentication cancelled.")
                return {"success": True, "tokens": TOKENS}

        controller.account.login = LoginFlow(controller.account, Client)
        server = start_server(controller, port=0, allowed_ip="127.0.0.1")

        class Proxy(BaseHTTPRequestHandler):
            def do_GET(self):
                self.forward()

            def do_POST(self):
                self.forward()

            def forward(self):
                prefix = "/api/hassio_ingress/fixture/"
                if not self.path.startswith(prefix):
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                with requests.request(
                    self.command, f"http://127.0.0.1:{server.server_port}/" + self.path[len(prefix):],
                    headers={**{key: value for key, value in self.headers.items() if key.lower() != "host"},
                             "X-Ingress-Path": prefix.rstrip("/")},
                    data=self.rfile.read(length) if length else None, timeout=5,
                ) as response:
                    self.send_response(response.status_code)
                    for key, value in response.headers.items():
                        if key.lower() not in ("connection", "server", "date", "transfer-encoding"):
                            self.send_header(key, value)
                    self.end_headers()
                    self.wfile.write(response.content)

            def log_message(self, *args):
                pass

        proxy = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
        proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        proxy_thread.start()
        url = f"http://127.0.0.1:{proxy.server_port}/api/hassio_ingress/fixture/"
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(executable_path="/usr/bin/chromium", args=["--no-sandbox"])
                try:
                    page = await browser.new_page(viewport={"width": 390, "height": 844})
                    errors, escaped = [], []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.on("request", lambda request: escaped.append(request.url)
                            if urlsplit(request.url).scheme in ("http", "https") and
                            not urlsplit(request.url).path.startswith("/api/hassio_ingress/fixture/") else None)
                    await page.route("**/api/ai-settings", ai_settings_route)
                    await page.goto(url, wait_until="networkidle")
                    assert await page.get_by_role("link", name="Aula account login", exact=True).count() == 0
                    assert await page.locator('[data-tab="unread"]').count() == 0
                    await page.get_by_role("link", name="Settings", exact=True).click()
                    await page.get_by_role("heading", name="Settings", exact=True).wait_for()
                    account = page.locator(".account-card")
                    await account.get_by_text("Signed in via Home Assistant").wait_for()
                    assert await page.evaluate(
                        "document.querySelector('#page .page-title').nextElementSibling.classList.contains('account-card')")
                    refreshes = []
                    patch.setattr(controller, "request_refresh", lambda *a, **k: refreshes.append(1),
                                  raising=False)
                    await account.get_by_role("button", name="Refresh feed").click()
                    await account.locator(".account-sync").filter(has_text="Refresh queued").or_(
                        account.locator(".account-sync[role=alert]")).wait_for()
                    assert "Refresh queued" in await account.locator(".account-sync").inner_text()
                    await page.get_by_role("link", name="AI ›", exact=True).click()
                    await page.get_by_role("heading", name="AI", exact=True).wait_for()
                    assert await page.locator(".ai-source-select").count() == 0
                    ai_select = page.locator(".ai-settings-select")
                    await ai_select.wait_for()
                    assert await ai_select.locator("option").count() == 7
                    await ai_select.select_option("anthropic")
                    await page.get_by_role("button", name="Add", exact=True).click()
                    await page.get_by_label("Anthropic (my API key) API key").fill("test-key-value")
                    await page.get_by_role("button", name="Save key", exact=True).click()
                    await page.locator("li", has_text="Anthropic").get_by_text("claude").or_(
                        page.locator("li", has_text="Anthropic").get_by_text("m", exact=True)).first.wait_for()
                    assert ai_settings_posts == [{"ai_credential": {"source": "anthropic", "model": "m", "api_key": "test-key-value"}}]
                    assert "test-key-value" not in await page.content()
                    ai_settings_posts.clear()
                    await ai_select.select_option("ai_task.google_ai_task")
                    await page.get_by_role("button", name="Add", exact=True).click()
                    await ai_select.select_option("ai_task.openai_ai_task")
                    await page.get_by_role("button", name="Add", exact=True).click()
                    await page.get_by_role("button", name="Move up ai_task.openai_ai_task", exact=True).click()
                    await page.get_by_role("button", name="Save providers", exact=True).click()
                    await page.wait_for_function(
                        "() => document.querySelector('.ai-settings-status').textContent.includes('Saved')"
                    )
                    assert ai_settings_posts == [{"ai_providers": ["anthropic", "ai_task.openai_ai_task", "ai_task.google_ai_task"]}]
                    assert "sk-" not in await page.content()
                    await page.get_by_role("link", name="Settings", exact=True).click()
                    await page.get_by_role("link", name="Aula account login ›", exact=True).click()
                    await page.wait_for_url("**/api/hassio_ingress/fixture/account")
                    assert await page.locator(".brand-mark").evaluate("image => image.complete && image.naturalWidth === 256")
                    assert await page.locator('link[rel="icon"]').get_attribute("href") == "icon.svg"
                    assert "Keep your integrations" in await page.locator("#sync-status").inner_text()
                    await page.get_by_label("MitID username", exact=True).fill("synthetic-guardian")
                    await page.get_by_role("button", name="Start MitID login", exact=True).click()
                    await page.get_by_role("button", name="Guardian identity", exact=True).click()
                    await page.locator("#qr").wait_for(state="visible")
                    assert (await page.locator("#qr").get_attribute("src")).startswith("data:image/svg+xml;base64,")
                    await responsive(page)
                    approved.set()
                    await page.wait_for_function("() => document.querySelectorAll('#children p').length === 2")
                    controller.poll_cycle()
                    await page.wait_for_function("() => document.querySelector('#sync-status').textContent.includes('Last successful direct sync')")
                    photo_slug = controller.config.children[0].slug
                    controller.avatars.session = AvatarSession()
                    assert controller.avatars.refresh(photo_slug, ["https://media.example/child.png?sig=1"])
                    controller.publish_feed()
                    assert controller.account.connection.value
                    assert "private-access-value" not in await page.content()

                    approved.clear()
                    await page.get_by_role("button", name="Start MitID login", exact=True).click()
                    await page.get_by_role("button", name="Cancel login", exact=True).click()
                    await page.wait_for_function("() => document.querySelector('#login-status').textContent.toLowerCase().includes('cancelled')")
                    controller.account.login.worker.join(3)
                    mode["fail"] = True
                    await page.get_by_role("button", name="Start MitID login", exact=True).click()
                    await page.wait_for_function("() => document.querySelector('#login-status').textContent.toLowerCase().includes('login failed')")
                    assert "raw authentication material" not in await page.content()
                    await page.get_by_role("link", name="Back to Settings", exact=True).click()
                    await page.get_by_role("heading", name="Settings", exact=True).wait_for()
                    await page.get_by_role("link", name="Aula home", exact=True).click()
                    await page.get_by_role("heading", name="Feed", exact=True).wait_for()
                    assert await page.locator(".post").count() >= 2
                    await page.goto(url, wait_until="networkidle")
                    await page.wait_for_function(
                        "() => [...document.querySelectorAll('.presence-photo')].some(img => img.complete && img.naturalWidth === 1)")
                    assert await page.locator(".presence-photo").count() == 1
                    author = (await page.locator(".post .author").first.inner_text()).removeprefix("By ").strip()
                    note_author(author, {"profilePicture": {"url": "https://media.example/author.png?sig=1"}})
                    assert controller.avatars.refresh_authors() == 1
                    controller.publish_feed()
                    await page.goto(url, wait_until="networkidle")
                    photo = page.locator(".post .author-photo").first
                    await photo.scroll_into_view_if_needed()
                    await page.wait_for_function(
                        "() => [...document.querySelectorAll('.author-photo')].some(img => img.complete && img.naturalWidth === 1)")
                    await page.evaluate("() => window.scrollTo(0, 0)")
                    await page.locator('[data-tab="calendar"]').click()
                    await page.get_by_role("heading", name="Calendar", exact=True).wait_for()
                    await page.locator(".calendar-agenda").wait_for()
                    assert await page.locator(".calendar-entry, .agenda-empty").count() >= 1
                    await page.get_by_role("link", name="Settings", exact=True).click()
                    await page.get_by_role("heading", name="Settings", exact=True).wait_for()
                    await page.get_by_role("link", name="Calendar subscription ›", exact=True).wait_for()
                    appearance = page.get_by_label("Appearance", exact=True)
                    await page.emulate_media(color_scheme="dark")
                    await appearance.select_option("light")
                    assert await page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(237, 241, 244)"
                    await page.reload(wait_until="networkidle")
                    await page.get_by_label("Appearance", exact=True).wait_for()
                    assert await page.get_by_label("Appearance", exact=True).input_value() == "light"
                    await page.emulate_media(color_scheme="light")
                    await page.get_by_label("Appearance", exact=True).select_option("dark")
                    assert await page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(14, 17, 19)"
                    await page.get_by_label("Appearance", exact=True).select_option("system")
                    assert await page.get_by_role("link", name="Calendar subscription ›", exact=True).count() == 1
                    assert await page.get_by_role("link", name="Aula account login ›", exact=True).count() == 1
                    await page.get_by_role("link", name="Calendar subscription ›", exact=True).click()
                    await page.get_by_role("heading", name="Event categories", exact=True).wait_for()
                    lessons = page.locator(".calendar-categories input[type=checkbox]").first
                    assert await lessons.is_checked()
                    await lessons.uncheck()
                    await page.locator(".calendar-categories [role=status]", has_text="1 category excluded").wait_for()
                    assert controller.profiles.calendar_excluded_categories() == {await lessons.get_attribute("value")}
                    await lessons.check()
                    await page.locator(".calendar-categories [role=status]", has_text="All categories are included").wait_for()
                    assert not controller.profiles.calendar_excluded_categories()
                    await page.get_by_role("link", name="Settings", exact=True).click()
                    await page.get_by_role("heading", name="Settings", exact=True).wait_for()
                    controller.client.list_services = lambda: {"notify": ["mobile_app_test_phone", "send_message"]}
                    notify_calls = []
                    controller.client.call_service = lambda *args, **kwargs: notify_calls.append(args) or []
                    await page.get_by_role("link", name="Notifications ›", exact=True).click()
                    await page.get_by_label("Add notify target").select_option("service:notify.mobile_app_test_phone")
                    await page.locator(".notification-add-row").get_by_role("button", name="Add", exact=True).click()
                    await page.locator(".notification-categories input[value=lesson]").check()
                    await page.locator(".notification-categories").get_by_label("Naps", exact=True).check()
                    await page.get_by_role("button", name="Save notifications", exact=True).click()
                    await page.locator(".notification-status", has_text="Saved. New items").wait_for()
                    assert controller.notifier.settings()["targets"] == ["service:notify.mobile_app_test_phone"]
                    assert "lesson" in controller.notifier.settings()["categories"]
                    assert "nap" in controller.notifier.settings()["categories"]
                    await page.locator(".notification-target").get_by_role("button", name="Remove", exact=False).click()
                    await page.get_by_role("button", name="Save notifications", exact=True).click()
                    await page.locator(".notification-status", has_text="Home Assistant notifications are off").wait_for()
                    assert controller.notifier.settings()["targets"] == []
                    assert "lesson" in controller.notifier.settings()["categories"]
                    await page.get_by_label("Add notify target").select_option("service:notify.mobile_app_test_phone")
                    await page.locator(".notification-add-row").get_by_role("button", name="Add", exact=True).click()
                    await page.get_by_role("button", name="Save notifications", exact=True).click()
                    await page.locator(".notification-status", has_text="Saved. New items").wait_for()
                    await page.get_by_role("button", name="Send test", exact=True).click()
                    await page.locator(".notification-status", has_text="Test sent to 1 target").wait_for()
                    assert notify_calls[-1][:2] == ("notify", "mobile_app_test_phone")
                    await push_settings(page, controller)
                    await responsive(page)
                    await page.get_by_role("link", name="Settings", exact=True).click()
                    await page.get_by_role("heading", name="Settings", exact=True).wait_for()
                    await responsive(page)
                    await calendar_agenda(page, url)
                    await chronological_agenda(page, url)
                    await list_back_position(page, url)
                    await presentation(page, url)
                    assert not escaped, escaped
                    await media_details(page, url, controller)
                    await installed_ios_download(browser, url)
                    await post_photo_grid(browser, url)
                    await album_tagging(browser, url)
                    await image_recovery(browser, url)
                    await mcp_settings(browser, url)
                    await sickness_reporting(browser, url)
                    await retired_siri_settings(browser, url)
                    await sync_settings_page(browser, url)
                    await mark_read_on_open(browser, url)
                    await notification_history(browser, url)
                    await presence_strip(page, url)
                    await related_reading(page, url)
                    await post_collapse(page, url)
                    await update_toast(page, url)
                    assert not errors, errors
                    assert set(escaped) == {
                        "https://media.example.test/photo.png",
                        "https://media.example.test/missing.png",
                    }, escaped
                finally:
                    if errors:
                        print("Browser errors:", errors)
                    await browser.close()
            print("Synthetic ingress: login/sync, rich summaries, latest-first chat pagination, translation states/originals and mobile layouts passed.")
        finally:
            approved.set()
            proxy.shutdown()
            proxy.server_close()
            server.shutdown()
            server.server_close()
            controller.close()


if __name__ == "__main__":
    asyncio.run(verify())
