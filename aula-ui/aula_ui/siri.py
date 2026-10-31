"""Narrow Apple Shortcuts connector; never accepts arbitrary Aula API methods."""

import json
import re
import threading
import time
from collections import deque

from .messages import date_key, strip_html


def spoken_text(value):
    text = strip_html(str(value or ""))
    text = re.sub(r"\[([^\]]+)\]\(https?://[^)]+\)", r"\1", text)
    text = re.sub(r"(?m)^\s*(?:#{1,6}\s+|[-*]\s+)", "", text)
    return re.sub(r"[*_`]+", "", text).strip()


class SiriConnector:
    def __init__(self, controller, sick_days):
        self.controller = controller
        self.sick_days = sick_days
        self.lock = threading.Lock()
        self.requests = deque()

    def admit(self):
        with self.lock:
            now = time.monotonic()
            while self.requests and now - self.requests[0] >= 60:
                self.requests.popleft()
            if len(self.requests) >= 20:
                return False
            self.requests.append(now)
            return True

    def settings(self):
        base = self.controller.config.public_url.rstrip("/")
        return {
            "enabled": self.controller.profiles.siri_enabled(),
            "endpoint": base + "/api/siri/command" if base else "",
            "children": [{"id": profile["slug"], "name": profile["display_name"]}
                         for profile in self.controller.profiles.snapshot()],
        }

    def _child(self, value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Supply a child name or id")
        key = value.strip().casefold()
        matches = [profile for profile in self.controller.profiles.snapshot()
                   if key in {profile["slug"].casefold(), profile["display_name"].casefold()}]
        if len(matches) != 1:
            raise ValueError("Child is unknown or ambiguous; use the child id from Siri settings")
        return matches[0]

    def command(self, value):
        if not isinstance(value, dict):
            raise ValueError("Supply a JSON command")
        action = value.get("action")
        if action == "recap":
            if not set(value) <= {"action", "child", "count"}:
                raise ValueError("Unsupported recap fields")
            child = self._child(value.get("child"))
            count = value.get("count", 3)
            if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 5:
                raise ValueError("Choose between one and five posts")
            feed = json.loads(self.controller.feed_json)
            items = [item for item in feed.get("items", [])
                     if item.get("content_type") == "Post" and child["slug"] in item.get("child_slugs", [])]
            items.sort(key=lambda item: (date_key(item), item["id"]), reverse=True)
            posts = []
            for item in items[:count]:
                summary = item.get("summary")
                posts.append({
                    "id": item["id"], "title": spoken_text(item.get("translated_title") or item.get("title")),
                    "date": str(item.get("date") or ""),
                    "text": spoken_text(summary or item.get("original_text")),
                    "summary_available": bool(summary),
                })
            speech = [f"Here are the latest {len(posts)} cached Aula posts for {child['display_name']}."
                      if posts else f"No cached Aula posts were found for {child['display_name']}."]
            for post in posts:
                speech.append(f"{post['title']}. {post['date'][:10]}.")
                if not post["summary_available"]:
                    speech.append("Summary not ready. Reading the original text.")
                speech.append(post["text"] or "No text is available.")
            if feed.get("errors"):
                speech.append("Some Aula syncing is currently unavailable. These are the last saved posts.")
            return {"speech": "\n\n".join(speech), "posts": posts, "cached": True,
                    "feed_published_at": feed.get("updated_at"), "child": child["slug"]}
        if action == "sick_status":
            if set(value) != {"action", "child"}:
                raise ValueError("Supply action and child only")
            child = self._child(value["child"])
            status = self.sick_days.snapshot()
            entry = next((entry for entry in status["children"] if entry["slug"] == child["slug"]), None)
            if entry is None:
                raise LookupError(f"Sickness status is unavailable for {child['display_name']}")
            return {**entry, "date": status["date"], "valid_until": status["valid_until"],
                    "notice": status["notice"],
                    "speech": (f"{child['display_name']} is already reported sick today." if entry["sick"]
                               else f"{child['display_name']} is not reported sick today." if entry["can_report"]
                               else f"Sickness reporting is unavailable for {child['display_name']}. "
                                    + entry.get("reason", "Check the app."))}
        if action == "sick_today":
            if set(value) != {"action", "child", "date", "confirmed"} or value["confirmed"] is not True:
                raise ValueError("Confirm the child and today's date before reporting sickness")
            child = self._child(value["child"])
            status = self.sick_days.report(child["slug"], value["date"])
            return {"speech": f"{child['display_name']} is reported sick for {value['date']} only. "
                              "A sick day has been added to your calendar. "
                              "Notify the school teacher separately if needed.",
                    "date": value["date"], "child": child["slug"], "sick": True,
                    "valid_until": status["valid_until"]}
        raise ValueError("Supported actions: recap, sick_status, sick_today")
