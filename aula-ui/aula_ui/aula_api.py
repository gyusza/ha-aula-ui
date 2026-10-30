"""Per-child message threads straight from the Aula API.

The ``aula`` / ``aula_easyiq`` message *sensors* only ever expose a single
globally-latest message with no indication of which child it concerns, so
every child ends up showing the same thread. The Aula API itself is much
richer: ``messaging.getThreads`` returns a ``regardingChildren`` list on every
thread, giving exact per-child attribution (a thread may regard more than one
child), and ``messaging.getMessagesForThread`` returns the full conversation.

Calls go through a client exposing ``call_service("aula", "api_call", ...)``:
the add-on's own ``DirectAulaClient`` (direct Aula login) or, in legacy mode,
Home Assistant's former ``aula.api_call`` service.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

from .diagnostics import describe
from .messages import normalise_message
from .avatars import note_author

_LOGGER = logging.getLogger(__name__)

THREADS_URI = (
    "?method=messaging.getThreads&sortOn=date&orderDirection=desc&page={page}"
)
THREAD_MESSAGES_URI = (
    "?method=messaging.getMessagesForThread&threadId={thread_id}&page={page}"
)
API_TIMEOUT = 60


class AulaApiError(Exception):
    """Raised when the Aula API proxy call fails."""


class AulaRateLimited(AulaApiError):
    """Aula answered HTTP 429; callers must pause instead of retrying."""

    # Expected operational state: cached content is kept and syncing resumes later.
    expected = True


class AulaBudgetExceeded(AulaRateLimited):
    """The add-on's own hourly request budget is spent; Aula was not contacted."""


def _call(client: Any, uri: str) -> dict[str, Any]:
    """Make one Aula API call through ``client`` and return the decoded payload."""
    try:
        response = client.call_service(
            "aula",
            "api_call",
            {"uri": uri},
            return_response=True,
            timeout=API_TIMEOUT,
        )
    except AulaApiError:
        # Already user-facing; keep the type so rate limits stay recognisable.
        raise
    except Exception as err:  # noqa: BLE001 - surface any transport error
        raise AulaApiError(f"Aula request failed: {err}") from err

    payload = response.get("service_response") if isinstance(response, dict) else None
    if not isinstance(payload, dict):
        raise AulaApiError("Aula request returned no payload")

    status = payload.get("status")
    if isinstance(status, dict) and status.get("code") not in (0, None):
        raise AulaApiError(
            f"Aula API error {status.get('code')}: {status.get('message')}"
        )
    return payload


def fetch_threads(client: Any, pages: int = 2) -> list[dict[str, Any]]:
    """Return raw thread dicts, newest first, across ``pages`` pages."""
    threads: list[dict[str, Any]] = []
    for page in range(max(1, pages)):
        payload = _call(client, THREADS_URI.format(page=page))
        data = payload.get("data") or {}
        batch = data.get("threads")
        if not isinstance(batch, list):
            raise AulaApiError("Aula threads response has no threads list")
        if not batch:
            break
        threads.extend(item for item in batch if isinstance(item, dict))
        if not data.get("moreMessagesExist"):
            break
    return threads


def thread_children(thread: dict[str, Any]) -> list[str]:
    """Return the display names of the children a thread regards."""
    return [
        str(entry.get("displayName") or "").strip()
        for entry in (thread.get("regardingChildren") or [])
        if isinstance(entry, dict) and entry.get("displayName")
    ]


def thread_matches_child(thread: dict[str, Any], child: Any) -> bool:
    """Decide whether ``thread`` concerns ``child``.

    Matches on the Aula profile id when configured, otherwise on the
    configured Aula display name (or, failing that, the child's own name)
    appearing in one of the thread's ``regardingChildren`` entries.
    """
    wanted_id = str(getattr(child, "aula_child_id", "") or "").strip()
    if wanted_id:
        for entry in thread.get("regardingChildren") or []:
            if isinstance(entry, dict) and str(entry.get("profileId") or "") == wanted_id:
                return True
        return False

    needle = (getattr(child, "aula_name", "") or child.name or "").strip().lower()
    if not needle:
        return False
    return any(needle in name.lower() for name in thread_children(thread))


def threads_for_child(threads: list[dict[str, Any]], child: Any) -> list[dict[str, Any]]:
    """Return every thread concerning ``child``, newest first."""
    return [thread for thread in threads if thread_matches_child(thread, child)]


def _thread_preview(thread: dict[str, Any]) -> str:
    latest = thread.get("latestMessage")
    if isinstance(latest, dict):
        text = latest.get("text")
        if isinstance(text, dict):
            return str(text.get("html") or text.get("text") or "")
        if text:
            return str(text)
    return ""


def _thread_date(thread: dict[str, Any]) -> str:
    latest = thread.get("latestMessage")
    if isinstance(latest, dict) and latest.get("sendDateTime"):
        return str(latest["sendDateTime"])
    return str(thread.get("startedTime") or "")


def message_from_thread_summary(thread: dict[str, Any]) -> dict[str, Any] | None:
    """Build a normalised message from the thread list entry alone.

    Used as a fallback when the full conversation cannot be fetched.
    """
    creator = thread.get("creator") or {}
    latest = thread.get("latestMessage") or {}
    sender = latest.get("sender") or creator
    for profile in (creator, latest.get("sender")):
        if isinstance(profile, dict):
            note_author(profile.get("fullName") or profile.get("displayName"), profile)
    return normalise_message(
        {
            "id": latest.get("id"),
            "subject": thread.get("subject"),
            "text": _thread_preview(thread),
            "sender": (sender.get("fullName") or sender.get("displayName"))
            if isinstance(sender, dict) else str(sender),
            "date": _thread_date(thread),
            "attachments": latest.get("attachments"),
            "hasAttachments": latest.get("hasAttachments"),
        }
    )


def fetch_thread_messages(
    client: Any, thread_id: Any, limit: int | None = None
) -> list[dict[str, Any]]:
    """Fetch all pages, newest first, deduplicating overlapping page boundaries.

    ``limit`` remains available for legacy callers requesting a preview.
    Absence of pagination metadata is handled by probing the next page;
    repeated unpaginated responses stop without duplicating messages.
    Explicitly unfinished/repeated pages fail rather than masquerading as a
    complete conversation.
    """
    messages: list[dict[str, Any]] = []
    seen: set[str] = set()
    seen_pages: set[str] = set()
    page = 0
    subject = ""
    while True:
        payload = _call(client, THREAD_MESSAGES_URI.format(
            thread_id=quote(str(thread_id), safe=""), page=page,
        ))
        data = payload.get("data") or {}
        raw_messages = data.get("messages")
        if not isinstance(raw_messages, list):
            raise AulaApiError("Aula thread response has no messages list")
        subject = str(data.get("subject") or subject)
        more = next((data[key] for key in (
            "moreMessagesExist", "hasMoreMessages", "hasMore", "hasNextPage"
        ) if key in data), None)
        if isinstance(more, str):
            more = more.lower() in {"true", "1", "yes"}
        signature = repr(raw_messages)
        if signature in seen_pages:
            if more:
                raise AulaApiError("Aula thread pagination repeated an unfinished page")
            break
        seen_pages.add(signature)
        if not raw_messages:
            if more:
                raise AulaApiError("Aula thread pagination returned an empty unfinished page")
            break
        for raw in raw_messages:
            # Aula labels the current body of an edited message MessageEdited,
            # not Message. Recipient-change/system entries are not messages.
            if not isinstance(raw, dict) or raw.get("messageType", "Message") not in {
                "Message", "MessageEdited",
            }:
                continue
            text = raw.get("text")
            body = (text.get("html") or text.get("text") or "") if isinstance(text, dict) else text
            sender = raw.get("sender") or {}
            if isinstance(sender, dict):
                note_author(sender.get("fullName") or sender.get("displayName"), sender)
            normalised = normalise_message({
                "id": raw.get("id"), "subject": subject, "text": body or "",
                "sender": (sender.get("fullName") or sender.get("displayName") or "")
                if isinstance(sender, dict) else str(sender),
                "date": raw.get("sendDateTime"),
                "attachments": raw.get("attachments"),
                "hasAttachments": raw.get("hasAttachments"),
            })
            if normalised and normalised["id"] not in seen:
                seen.add(normalised["id"])
                messages.append(normalised)
                if limit is not None and len(messages) >= max(1, limit):
                    return messages
        if more is False or more == 0:
            break
        total = data.get("totalMessages") or data.get("totalCount")
        if isinstance(total, int) and len(messages) >= total:
            break
        page += 1
    return messages


MARKABLE_MESSAGE_TYPES = {"Message", "MessageEdited", "MessageDeleted", "AutoReply"}
MARK_READ_URI = "?method=messaging.setLastReadMessage"
MARK_READ_MAX_PAGES = 10


def mark_thread_read(client: Any, thread_id: Any) -> bool:
    """Move Aula's read marker of a thread to its newest message, as Aula does on opening it.

    Returns ``False`` when it was already read there or holds no message. Only threads
    in the guardian's own mailbox are marked; common/OTP inboxes need extra IDs.
    """
    if not callable(getattr(client, "post", None)):
        raise AulaApiError("Marking as read needs the add-on's own Aula login.")
    newest = None
    data: dict[str, Any] = {}
    for page in range(MARK_READ_MAX_PAGES):
        data = _call(client, THREAD_MESSAGES_URI.format(
            thread_id=quote(str(thread_id), safe=""), page=page,
        )).get("data") or {}
        owner = data.get("mailBoxOwner")
        if isinstance(owner, dict) and owner.get("mailBoxOwnerType") not in (None, "institutionProfile"):
            raise AulaApiError("This conversation is in a shared inbox; mark it read in Aula.")
        newest = next((message for message in data.get("messages") or []
                       if isinstance(message, dict) and message.get("id")
                       and message.get("messageType", "Message") in MARKABLE_MESSAGE_TYPES), None)
        if newest is not None or not data.get("moreMessagesExist"):
            break
    if newest is None:
        return False
    if page == 0 and data.get("lastReadMessageId") == newest["id"]:
        return False
    thread = int(thread_id) if str(thread_id).isdigit() else thread_id
    payload = client.post(MARK_READ_URI, {
        "threadId": thread, "messageId": newest["id"], "commonInboxId": None, "otpInboxId": None,
    })
    if not isinstance(payload, dict) or (payload.get("status") or {}).get("code", 0) != 0:
        raise AulaApiError("Aula did not accept the read marker.")
    return True


class AulaThreadSource:
    """Fetches the thread list once per cycle and serves it per child."""

    def __init__(self, client: Any, pages: int = 2, context_messages: int = 5) -> None:
        self._client = client
        self._pages = pages
        self._context_messages = context_messages
        self._threads: list[dict[str, Any]] = []
        self.available = False

    @property
    def threads(self) -> list[dict[str, Any]]:
        return list(self._threads)

    def refresh(self) -> bool:
        """Re-fetch threads. A successful empty list is not an API failure."""
        try:
            self._threads = fetch_threads(self._client, self._pages)
        except AulaApiError as err:
            _LOGGER.warning("Could not read threads from the Aula API: %s", describe(err))
            self._threads = []
            self.available = False
            return False

        self.available = True
        _LOGGER.debug("Fetched %d Aula threads", len(self._threads))
        return True

    def messages_for(self, child: Any) -> list[dict[str, Any]]:
        """Return the newest thread's messages for ``child``, newest first."""
        matches = threads_for_child(self._threads, child)
        if not matches:
            _LOGGER.info(
                "No Aula threads regarding child=%s",
                child.slug,
            )
            return []

        thread = matches[0]
        _LOGGER.debug(
            "event=threads.matched child=%s count=%d",
            child.slug,
            len(matches),
        )
        try:
            messages = fetch_thread_messages(
                self._client, thread.get("id"), self._context_messages
            )
        except AulaApiError as err:
            _LOGGER.warning(
                "Could not read thread %s for %s: %s", thread.get("id"), child.slug, describe(err)
            )
            messages = []

        if messages:
            return messages

        summary = message_from_thread_summary(thread)
        return [summary] if summary else []

    def mark_read(self, thread_id: Any) -> None:
        for thread in self._threads:
            if str(thread.get("id")) == str(thread_id):
                thread["read"] = True

    def unread_count_for(self, child: Any) -> int:
        """Number of unread threads concerning ``child``."""
        return sum(
            1
            for thread in threads_for_child(self._threads, child)
            if not thread.get("read", True)
        )
