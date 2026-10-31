"""Add-on configuration loading and validation."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_LOGGER = logging.getLogger(__name__)

SLUG_RE = re.compile(r"^[a-z0-9_]+$")

DEFAULT_SUPERVISOR_URL = "http://supervisor/core/api"
DEFAULT_TIMEZONE = "Europe/Copenhagen"

# Still parsed for older installs, but no longer offered in the App schema:
# children are discovered after the in-app Aula login (and remembered in
# profiles.json) and the AI task entity is chosen in the app's Settings.
RETIRED_OPTIONS = (
    "children", "data_source", "message_source", "context_messages",
    "ai_task_entity", "ha_url", "ha_token", "llm_provider", "model", "llm_command",
)


@dataclass(frozen=True)
class ChildConfig:
    """Configuration for a single child."""

    name: str
    slug: str
    messages_sensor: str
    weekplan_sensor: str | None = None
    calendars: tuple[str, ...] = ()
    aula_name: str = ""
    aula_child_id: str = ""
    institution_type: str = "school"

    @property
    def thread_id(self) -> str:
        """Thread identifier used for pinning."""
        return self.slug


@dataclass(frozen=True)
class AddonConfig:
    """Resolved add-on configuration."""

    ha_url: str
    ha_token: str
    poll_interval: int = 900
    context_messages: int = 5
    event_lookahead_days: int = 21
    message_source: str = "auto"
    data_source: str = "direct"
    thread_pages: int = 2
    llm_provider: str = "ha_ai_task"
    ai_task_entity: str = ""
    ai_task_entities: tuple[str, ...] = ()
    anthropic_api_key: str = ""
    google_api_key: str = ""
    cloudflare_account_id: str = ""
    cloudflare_api_token: str = ""
    anthropic_model: str = ""
    google_model: str = ""
    cloudflare_model: str = ""
    ai_providers: tuple[str, ...] = ()
    model: str = "claude-sonnet-4-5"
    llm_command: str = ""
    log_level: str = "info"
    log_color: bool = True
    timezone: str = DEFAULT_TIMEZONE
    data_dir: str = "/data"
    access_team_domain: str = ""
    access_aud: str = ""
    access_allowed_emails: tuple[str, ...] = ()
    public_url: str = ""
    mcp_key: str = ""
    mcp_allow_writes: bool = False
    children: tuple[ChildConfig, ...] = field(default_factory=tuple)


DEFAULT_PUBLIC_URL = ""


def _public_url(value: Any, option: str = "public_url") -> str:
    text = _as_str(value).rstrip("/")
    if text and not re.fullmatch(r"https?://[^\s/?#]+(/[^\s?#]*)?", text):
        raise ConfigError(f"Option '{option}' must be an http(s) URL or empty")
    return text


class ConfigError(Exception):
    """Raised when the add-on configuration is unusable."""


def _timezone(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("Option 'timezone' must be an IANA timezone such as Europe/Copenhagen")
    value = value.strip()
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as err:
        raise ConfigError(
            "Option 'timezone' must be an IANA timezone such as Europe/Copenhagen"
        ) from err
    return value


def _as_int(raw: Any, name: str, default: int, minimum: int, maximum: int) -> int:
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as err:
        raise ConfigError(f"Option '{name}' must be an integer, got {raw!r}") from err
    if not minimum <= value <= maximum:
        raise ConfigError(f"Option '{name}' must be between {minimum} and {maximum}, got {value}")
    return value


def _as_str(raw: Any) -> str:
    return "" if raw is None else str(raw).strip()


def _parse_children(raw_children: Any, direct: bool = False) -> tuple[ChildConfig, ...]:
    if direct and raw_children in (None, []):
        return ()
    if not isinstance(raw_children, list) or not raw_children:
        raise ConfigError("Option 'children' must be a non-empty list")

    children: list[ChildConfig] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_children):
        if not isinstance(raw, dict):
            raise ConfigError(f"children[{index}] must be a mapping")

        name = _as_str(raw.get("name"))
        slug = _as_str(raw.get("slug")).lower()
        messages_sensor = _as_str(raw.get("messages_sensor"))

        if not name:
            raise ConfigError(f"children[{index}].name is required")
        if not SLUG_RE.match(slug):
            raise ConfigError(
                f"children[{index}].slug must match [a-z0-9_]+ (got {slug!r})"
            )
        if slug in seen:
            raise ConfigError(f"Duplicate child slug {slug!r}")
        seen.add(slug)
        if not messages_sensor and not direct:
            raise ConfigError(f"children[{index}].messages_sensor is required")

        weekplan_sensor = _as_str(raw.get("weekplan_sensor")) or None
        raw_calendars = raw.get("calendars") or []
        if not isinstance(raw_calendars, list):
            raise ConfigError(f"children[{index}].calendars must be a list")
        calendars = tuple(
            cal for cal in (_as_str(item) for item in raw_calendars) if cal
        )

        institution_type = _as_str(raw.get("institution_type")) or "school"
        if institution_type not in ("school", "daycare"):
            raise ConfigError("institution_type must be school or daycare")
        children.append(
            ChildConfig(
                name=name,
                slug=slug,
                messages_sensor=messages_sensor,
                weekplan_sensor=weekplan_sensor,
                calendars=calendars,
                aula_name=_as_str(raw.get("aula_name")),
                aula_child_id=_as_str(raw.get("aula_child_id")),
                institution_type=institution_type,
            )
        )
    return tuple(children)


def load_options(path: str | None = None) -> dict[str, Any]:
    """Read the Supervisor-provided options file."""
    options_path = path or os.getenv("AULA_OPTIONS_FILE", "/data/options.json")
    if not os.path.exists(options_path):
        _LOGGER.warning("Options file %s not found, using defaults", options_path)
        return {}
    with open(options_path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ConfigError("options.json must contain a JSON object")
    return data


def build_config(options: dict[str, Any] | None = None) -> AddonConfig:
    """Build and validate the add-on configuration."""
    options = options if options is not None else load_options()
    retired = [key for key in RETIRED_OPTIONS if options.get(key) not in (None, "", [])]
    if retired:
        _LOGGER.info("Honouring retired option(s) for compatibility: %s", ", ".join(retired))

    # Home Assistant access: prefer explicit options, fall back to the
    # Supervisor proxy which needs no user-managed long-lived token.
    ha_url = _as_str(options.get("ha_url")) or _as_str(os.getenv("HA_URL"))
    ha_token = _as_str(options.get("ha_token")) or _as_str(os.getenv("HA_TOKEN"))
    supervisor_token = _as_str(os.getenv("SUPERVISOR_TOKEN"))

    if not ha_url:
        ha_url = DEFAULT_SUPERVISOR_URL
    if not ha_token:
        ha_token = supervisor_token

    if not ha_token:
        raise ConfigError(
            "No Home Assistant credentials available. Either run as a Supervisor "
            "add-on (homeassistant_api: true) or set the 'ha_token' option."
        )

    llm_provider = (_as_str(options.get("llm_provider")) or "ha_ai_task").lower()
    if llm_provider not in {"ha_ai_task", "anthropic", "command", "none"}:
        raise ConfigError(
            "Option 'llm_provider' must be one of "
            f"ha_ai_task/anthropic/command/none, got {llm_provider!r}"
        )

    if llm_provider in {"anthropic", "command"}:
        _LOGGER.warning("Retired direct AI provider ignored; using Home Assistant AI Tasks")
        llm_provider = "ha_ai_task"

    message_source = (_as_str(options.get("message_source")) or "auto").lower()
    if message_source not in {"auto", "aula_api", "sensor"}:
        raise ConfigError(
            "Option 'message_source' must be one of auto/aula_api/sensor, "
            f"got {message_source!r}"
        )

    data_source = (_as_str(options.get("data_source")) or "direct").lower()
    if data_source not in {"direct", "integration"}:
        raise ConfigError("Option 'data_source' must be direct or integration")

    log_level = (_as_str(options.get("log_level")) or "info").lower()
    if log_level not in {"trace", "debug", "info", "notice", "warning", "error", "fatal"}:
        raise ConfigError("Invalid log_level; choose trace/debug/info/warning/error/fatal")
    log_color = options.get("log_color", True)
    if not isinstance(log_color, bool):
        raise ConfigError("Option 'log_color' must be a boolean")

    mcp_key = _as_str(options.get("mcp_key"))
    if mcp_key and not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", mcp_key):
        raise ConfigError("Option 'mcp_key' must be 32-128 letters, digits, '-' or '_' (or empty to disable MCP)")
    mcp_allow_writes = options.get("mcp_allow_writes", False)
    if not isinstance(mcp_allow_writes, bool):
        raise ConfigError("Option 'mcp_allow_writes' must be a boolean")

    emails = options.get("access_allowed_emails") or []
    if not isinstance(emails, list) or not all(isinstance(e, str) for e in emails):
        raise ConfigError("Option 'access_allowed_emails' must be a list of email addresses")

    config = AddonConfig(
        ha_url=ha_url.rstrip("/"),
        ha_token=ha_token,
        poll_interval=_as_int(options.get("poll_interval"), "poll_interval", 900, 60, 86400),
        context_messages=_as_int(
            options.get("context_messages"), "context_messages", 5, 1, 25
        ),
        event_lookahead_days=_as_int(
            options.get("event_lookahead_days"), "event_lookahead_days", 21, 1, 120
        ),
        message_source=message_source,
        data_source=data_source,
        thread_pages=_as_int(options.get("thread_pages"), "thread_pages", 2, 1, 20),
        llm_provider=llm_provider,
        ai_task_entity=_as_str(options.get("ai_task_entity")),
        anthropic_api_key=_as_str(options.get("anthropic_api_key")),
        google_api_key=_as_str(options.get("google_api_key")),
        cloudflare_account_id=_as_str(options.get("cloudflare_account_id")),
        cloudflare_api_token=_as_str(options.get("cloudflare_api_token")),
        anthropic_model=_as_str(options.get("anthropic_model")),
        google_model=_as_str(options.get("google_model")),
        cloudflare_model=_as_str(options.get("cloudflare_model")),
        model="",
        llm_command="",
        log_level=log_level,
        log_color=log_color,
        timezone=_timezone(options.get("timezone", DEFAULT_TIMEZONE)),
        data_dir=_as_str(os.getenv("AULA_DATA_DIR")) or "/data",
        access_team_domain=_as_str(options.get("access_team_domain")).lower()
        .removeprefix("https://").rstrip("/"),
        access_aud=_as_str(options.get("access_aud")),
        access_allowed_emails=tuple(e.strip().lower() for e in emails if e.strip()),
        public_url=_public_url(options.get("public_url", DEFAULT_PUBLIC_URL)),
        mcp_key=mcp_key,
        mcp_allow_writes=mcp_allow_writes,
        children=_parse_children(options.get("children"), direct=data_source == "direct"),
    )

    if config.llm_provider == "ha_ai_task":
        _LOGGER.info("Using Home Assistant AI tasks; choose the entity in the app's Settings")

    return config
