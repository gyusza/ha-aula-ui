"""Content-free operation tracing for Supervisor-managed application logs."""

import logging
import re
import threading
import time
import traceback
import uuid
from collections import deque
from contextvars import ContextVar
from functools import wraps
from urllib.parse import urlsplit

correlation = ContextVar("log_correlation", default="-")
TRACE = 5
logging.addLevelName(TRACE, "TRACE")
_COLORS = {
    TRACE: "\033[90m",
    logging.DEBUG: "\033[36m",
    logging.INFO: "\033[32m",
    logging.WARNING: "\033[33m",
    logging.ERROR: "\033[31m",
    logging.CRITICAL: "\033[1;31m",
}
_secrets = deque(maxlen=128)
_lock = threading.Lock()
_URL = re.compile(r"https?://[^\s<>\"']+")
_CREDENTIAL = re.compile(
    r"(?i)((?:access_token|refresh_token|authorization|password|api_key|ha_token|"
    r"anthropic_api_key|google_api_key|cloudflare_api_token|x-api-key|x-goog-api-key|cookie|csrfp-token|code|state)[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s&,;]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s\"',;]+")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")


def stack_locations(tb):
    return " > ".join(f"{frame.filename.rsplit('/', 1)[-1]}:{frame.lineno}:{frame.name}"
                      for frame in traceback.extract_tb(tb))


def describe(error, limit=400):
    """One-line ``Type: message`` for logs; SafeFormatter still redacts secrets."""
    if isinstance(error, BaseException):
        message = " ".join(str(error).split())
        text = f"{type(error).__name__}: {message}" if message else type(error).__name__
    else:
        text = " ".join(str(error or "").split()) or "unknown error"
    return text if len(text) <= limit else text[:limit - 1] + "…"


def protect(*values):
    """Defence in depth for secrets appearing in legacy third-party errors."""
    with _lock:
        for value in values:
            if isinstance(value, str) and len(value) >= 4 and value not in _secrets:
                _secrets.append(value)


def safe_path(value):
    path = urlsplit(value).path
    path = re.sub(r"(/api/(?:aula_calendar|hassio_ingress)/)[^/]+", r"\1[redacted]", path)
    return path or "/"


def redact(value):
    with _lock:
        secrets = sorted(_secrets, key=len, reverse=True)
    for secret in secrets:
        value = value.replace(secret, "[redacted]")
    def safe_url(match):
        try:
            parsed = urlsplit(match[0])
            return parsed.scheme + "://" + (parsed.hostname or "") + safe_path(match[0])
        except ValueError:
            return "[invalid-url-redacted]"

    value = _URL.sub(safe_url, value)
    value = _BEARER.sub("Bearer [redacted]", value)
    value = _JWT.sub("[redacted]", value)
    value = _CREDENTIAL.sub(r"\1[redacted]", value)
    value = re.sub(r"/api/aula_calendar/[^/\s]+", "/api/aula_calendar/[redacted]", value)
    return value.replace("\r", "\\r").replace("\n", "\\n")


class SafeFormatter(logging.Formatter):
    converter = time.gmtime

    def __init__(self, *args, color=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.color = color

    def format(self, record):
        # Do not mutate shared records or include exception messages/source text.
        record = logging.makeLogRecord(record.__dict__.copy())
        record.correlation = correlation.get()
        record.exc_text = None
        text = redact(super().format(record)).replace("\033", "\\x1b")
        if self.color and record.levelno in _COLORS:
            label = f"[{record.levelname}]"
            text = text.replace(label, f"{_COLORS[record.levelno]}{label}\033[0m", 1)
        return text

    def formatException(self, exc_info):
        return f"{exc_info[0].__name__} at {stack_locations(exc_info[2])}"


def observed(event, level=logging.INFO):
    """Trace timing/outcome without inspecting arguments, results or errors."""
    def decorate(function):
        logger = logging.getLogger(function.__module__)

        @wraps(function)
        def wrapped(*args, **kwargs):
            token = None
            if correlation.get() == "-":
                token = correlation.set(uuid.uuid4().hex[:12])
            started = time.monotonic()
            logger.log(level, "event=%s phase=start", event)
            try:
                result = function(*args, **kwargs)
            except Exception as error:
                if getattr(error, "expected", False) is True:
                    # Expected operational states (e.g. an Aula rate-limit pause) are
                    # reported once where they arise, not again by every traced layer.
                    logger.log(max(level, logging.DEBUG),
                               "event=%s phase=failed duration_ms=%d error_type=%s",
                               event, round((time.monotonic() - started) * 1000), type(error).__name__)
                    raise
                logger.warning("event=%s phase=failed duration_ms=%d error_type=%s stack=%s",
                               event, round((time.monotonic() - started) * 1000),
                               type(error).__name__, stack_locations(error.__traceback__))
                raise
            else:
                count = f" result_count={len(result)}" if isinstance(result, (list, tuple)) else ""
                logger.log(level, "event=%s phase=complete duration_ms=%d%s",
                           event, round((time.monotonic() - started) * 1000), count)
                return result
            finally:
                if token is not None:
                    correlation.reset(token)
        return wrapped
    return decorate
