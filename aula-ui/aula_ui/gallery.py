"""Aula photo albums and permission-checked tagging of configured children."""

from __future__ import annotations

import threading
import time
from typing import Any
from urllib.parse import urlencode

from .aula_api import AulaApiError, _call


def profile_context(client: Any, children: Any) -> tuple[list[str], dict[str, set[str]]]:
    """Guardian and child institution profile ids (Aula returns nothing without both), plus institution codes per child."""
    profiles = (_call(client, "?method=profiles.getProfilesByLogin").get("data") or {}).get("profiles")
    if not isinstance(profiles, list):
        raise AulaApiError("Aula profiles response has no profiles list")
    ids: list[str] = []
    codes: dict[str, set[str]] = {}
    for profile in profiles:
        for relation in profile.get("children") or []:
            for child in children:
                if not (str(relation.get("profileId")) == str(getattr(child, "aula_child_id", "")) or
                        (getattr(child, "aula_name", "") or child.name) in str(relation.get("name", ""))):
                    continue
                ids.append(str(relation["id"]))
                ids.extend(str(guardian["id"]) for guardian in profile.get("institutionProfiles") or []
                           if guardian.get("institutionCode") == relation.get("institutionCode"))
                codes.setdefault(child.slug, set()).add(str(relation.get("institutionCode")))
    return list(dict.fromkeys(ids)), codes


def institution_profile_ids(client: Any, children: Any) -> list[str]:
    return profile_context(client, children)[0]


def _query(method: str, ids: list[str], **params: Any) -> str:
    pairs = [("method", method), *params.items(), *(("filterInstProfileIds[]", value) for value in ids)]
    return "?" + urlencode(pairs)


def albums(client: Any, ids: list[str], limit: int = 50) -> Any:
    return _call(client, _query("gallery.getAlbums", ids, index=0, limit=limit, sortOn="mediaCreatedAt",
                                orderDirection="desc", filterBy="all")).get("data")


def media(client: Any, ids: list[str], album_id: Any, limit: int = 1000) -> Any:
    return _call(client, _query("gallery.getMedia", ids, albumId=album_id, index=0, limit=limit,
                                sortOn="uploadedAt", orderDirection="desc", filterBy="all")).get("data")


def shape(value: Any, depth: int = 0) -> Any:
    """Describe a payload's structure without exposing its content."""
    if isinstance(value, dict):
        return {key: shape(item, depth + 1) for key, item in list(value.items())[:40]} if depth < 6 else "dict"
    if isinstance(value, list):
        return [f"list[{len(value)}]", shape(value[0], depth + 1)] if value else "list[0]"
    if isinstance(value, str):
        return "url" if value.startswith("https://") else f"str[{len(value)}]"
    return type(value).__name__


FEED_ALBUMS = 15
CACHE_SECONDS = 600
SIZES = {"small": "smallThumbnailUrl", "medium": "mediumThumbnailUrl", "large": "largeThumbnailUrl",
         "thumb": "thumbnailUrl", "original": None}
_lock = threading.Lock()
_tag_lock = threading.Lock()
_cache: dict[Any, tuple[float, Any]] = {}


def _cached(key: Any, load: Any, ttl: float = CACHE_SECONDS) -> Any:
    with _lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
    value = load()
    with _lock:
        now = time.monotonic()
        # Album media entries are keyed per album; drop expired ones so browsing
        # many albums over days does not grow the cache without bound.
        for stale in [k for k, (stamp, _) in _cache.items() if now - stamp >= max(ttl, CACHE_SECONDS)]:
            del _cache[stale]
        _cache[key] = (now, value)
    return value


def _album_rows(client: Any, children: Any, fresh: bool = False) -> tuple[list[dict[str, Any]], dict[str, set[str]]]:
    def load() -> tuple[list[dict[str, Any]], dict[str, set[str]]]:
        ids, codes = profile_context(client, children)
        rows = albums(client, ids, FEED_ALBUMS * 2)
        rows = rows if isinstance(rows, list) else (rows or {}).get("albums") or []
        return [row for row in rows if isinstance(row, dict) and row.get("id") is not None][:FEED_ALBUMS], codes
    if fresh:
        with _lock:
            _cache.pop("albums", None)
    return _cached("albums", load)


def feed_items(client: Any, children: Any) -> list[dict[str, Any]]:
    """One feed item per recent album; photos are fetched only when an album is opened."""
    rows, codes = _album_rows(client, children, fresh=True)
    items = []
    for row in rows:
        creator = row.get("creator") if isinstance(row.get("creator"), dict) else {}
        groups = [group for group in row.get("sharedWithGroups") or [] if isinstance(group, dict)]
        institutions = {str(group.get("institutionCode")) for group in groups} | {str(creator.get("institutionCode"))}
        slugs = [child.slug for child in children if codes.get(child.slug, set()) & institutions]
        items.append({
            "id": f"album:{row['id']}", "title": str(row.get("title") or "").strip() or "Photo album",
            "original_text": str(row.get("description") or "").strip(),
            "sender": str(creator.get("name") or "Aula"), "date": str(row.get("creationDate") or ""),
            "content_type": "Album", "children": [child.name for child in children if child.slug in slugs],
            "child_slugs": slugs,
            "album": {"id": str(row["id"]), "thumbs": len(row.get("thumbnailsUrls") or []),
                      "groups": [str(group.get("name")) for group in groups if group.get("name")]},
        })
    return items


def _all_media(client: Any, children: Any, album_id: str) -> dict[str, Any]:
    def load() -> dict[str, Any]:
        ids, _ = profile_context(client, children)
        results: list[dict[str, Any]] = []
        count = 0
        header: dict[str, Any] = {}
        offset = 0
        while offset < 500:
            query = _query("gallery.getMedia", ids, albumId=album_id, index=offset, limit=100,
                           sortOn="uploadedAt", orderDirection="asc", filterBy="all")
            data = _call(client, query).get("data") or {}
            batch = data.get("results")
            header = data.get("album") or header
            count = int(data.get("mediaCount") or data.get("totalSize") or 0)
            if not isinstance(batch, list) or not batch:
                break
            results.extend(row for row in batch if isinstance(row, dict) and not row.get("isDeleted"))
            offset += len(batch)
            if offset >= count:
                break
        return {"album": header, "count": count, "results": results}
    return _cached(("media", album_id), load)


def album_detail(client: Any, children: Any, album_id: str) -> dict[str, Any]:
    data = _all_media(client, children, album_id)
    choices = tag_choices(client, children, data["album"])
    media = []
    for row in data["results"]:
        file = row.get("file") if isinstance(row.get("file"), dict) else {}
        media.append({"id": str(row.get("id")), "type": "video" if row.get("mediaType") == "video" else "image",
                      "name": str(file.get("name") or f"{row.get('id')}.jpg"),
                      "created": str(file.get("created") or ""),
                      "can_edit_tags": row.get("currentUserCanEditTags") is True,
                      "tagged_children": [choice["slug"] for choice in choices if any(
                          isinstance(tag, dict) and str(tag.get("id")) == choice["institution_profile_id"]
                          for tag in row.get("tags") or [])]})
    return {"id": album_id, "title": str((data["album"] or {}).get("title") or ""),
            "count": data["count"] or len(media), "media": media,
            "tag_choices": [{"slug": choice["slug"], "name": choice["name"]} for choice in choices]}


def tag_choices(client: Any, children: Any, album: dict[str, Any]) -> list[dict[str, str]]:
    profiles = (_call(client, "?method=profiles.getProfilesByLogin").get("data") or {}).get("profiles")
    if not isinstance(profiles, list):
        raise AulaApiError("Aula profiles response has no profiles list")
    institutions = {
        str(group.get("institutionCode")) for group in album.get("sharedWithGroups") or []
        if isinstance(group, dict) and group.get("institutionCode")
    }
    creator = album.get("creator") or {}
    if creator.get("institutionCode"):
        institutions.add(str(creator["institutionCode"]))
    choices = []
    for child in children:
        relations = {
            str(relation["id"]) for profile in profiles for relation in profile.get("children") or []
            if str(relation.get("profileId")) == str(child.aula_child_id)
            and str(relation.get("institutionCode")) in institutions and relation.get("id")
        }
        if len(relations) == 1:
            choices.append({"slug": child.slug, "name": child.name,
                            "institution_profile_id": next(iter(relations))})
    return choices


def set_child_tag(client: Any, children: Any, album_id: str, media_id: str,
                  child_slug: str, tagged: bool) -> dict[str, Any]:
    """Only explicit user choices may tag a configured child; never infer identity."""
    if (not isinstance(album_id, str) or not album_id.isdigit()
            or not isinstance(media_id, str) or not media_id.isdigit()
            or not isinstance(child_slug, str) or not isinstance(tagged, bool)):
        raise ValueError("Supply an album, photo, child and tagged boolean")
    if not callable(getattr(client, "post", None)):
        raise AulaApiError("Photo tagging needs the add-on's own Aula login")
    with _tag_lock:
        with _lock:
            _cache.pop(("media", album_id), None)
        data = _all_media(client, children, album_id)
        row = next((row for row in data["results"] if str(row.get("id")) == media_id), None)
        if row is None:
            raise LookupError("Photo not found in this album")
        if row.get("currentUserCanEditTags") is not True:
            raise PermissionError("Aula does not allow your account to edit tags on this photo")
        choice = next((choice for choice in tag_choices(client, children, data["album"])
                       if choice["slug"] == child_slug), None)
        if choice is None:
            raise ValueError("Child is not available for tagging in this album")
        present = any(isinstance(tag, dict) and str(tag.get("id")) == choice["institution_profile_id"]
                      for tag in row.get("tags") or [])
        if present != tagged:
            method = "gallery.addTag" if tagged else "gallery.removeTag"
            payload = client.post("?" + urlencode({"method": method}), {
                "mediaId": int(media_id), "instProfileId": int(choice["institution_profile_id"]),
            })
            with _lock:
                _cache.pop(("media", album_id), None)
            if (not isinstance(payload, dict) or (payload.get("status") or {}).get("code", 0) != 0
                    or payload.get("data") is not True):
                raise AulaApiError("Aula did not confirm the photo tag; reload the album before retrying")
        detail = album_detail(client, children, album_id)
        confirmed = next((photo for photo in detail["media"] if photo["id"] == media_id), None)
        if confirmed is None or (child_slug in confirmed["tagged_children"]) != tagged:
            raise AulaApiError("Aula has not confirmed the updated tag; reload the album before retrying")
        return confirmed


def media_url(client: Any, children: Any, album_id: str, media_id: str, size: str) -> tuple[str, str]:
    """Fresh signed URL (they expire within the hour) and download name for one album item."""
    if size not in SIZES:
        raise ValueError("Unknown size")
    for row in _all_media(client, children, album_id)["results"]:
        if str(row.get("id")) == media_id:
            file = row.get("file") if isinstance(row.get("file"), dict) else {}
            key = SIZES[size]
            return str((file.get("url") if key is None else row.get(key) or file.get("url")) or ""), str(file.get("name") or "")
    raise LookupError("Photo not found")


def cover_url(client: Any, children: Any, album_id: str, index: int) -> str:
    rows, _ = _album_rows(client, children)
    row = next((row for row in rows if str(row["id"]) == album_id), None)
    urls = (row or {}).get("thumbnailsUrls") or []
    if not 0 <= index < len(urls):
        raise LookupError("Cover not found")
    return str(urls[index])
