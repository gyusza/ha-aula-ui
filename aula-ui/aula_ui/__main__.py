"""Add-on entry point."""

from __future__ import annotations

import logging
import signal
import sys
import os
import threading
from types import FrameType

from .config import ConfigError, build_config
from .controller import AulaUIController
from .ha import HAError, HomeAssistantClient
from .access import AccessVerifier
from .web import start_server
from .calendar_bridge import install_bridge
from .diagnostics import TRACE, SafeFormatter, protect
from . import __version__

_LOG_LEVELS = {
    "trace": TRACE,
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "notice": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "fatal": logging.CRITICAL,
}

_LOGGER = logging.getLogger("aula_ui")


def _setup_logging(level: str, color: bool = True) -> None:
    logging.basicConfig(
        level=_LOG_LEVELS.get(level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    for handler in logging.getLogger().handlers:
        handler.setFormatter(SafeFormatter(
            "%(asctime)sZ [%(levelname)s] %(name)s [%(threadName)s] "
            "trace=%(correlation)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S",
            color=color and "NO_COLOR" not in os.environ,
        ))
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("http.client").setLevel(logging.WARNING)

    def thread_error(args):
        _LOGGER.critical("event=thread.unhandled_error",
                         exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    threading.excepthook = thread_error
    sys.excepthook = lambda kind, value, tb: _LOGGER.critical(
        "event=application.unhandled_error", exc_info=(kind, value, tb),
    )


def main() -> int:
    os.umask(0o077)
    _setup_logging("info")
    try:
        config = build_config()
    except ConfigError as err:
        _LOGGER.error("Invalid configuration error_type=%s; check add-on options", type(err).__name__)
        return 1

    _setup_logging(config.log_level, config.log_color)
    protect(config.ha_token)
    _LOGGER.info("event=application.start version=%s log_level=%s source=%s",
                 __version__, config.log_level, config.data_source)
    install_bridge()
    _LOGGER.info(
        "Aula UI starting: %s child(ren), llm_provider=%s",
        len(config.children),
        config.llm_provider,
    )

    client = HomeAssistantClient(config.ha_url, config.ha_token)
    try:
        client.wait_until_ready()
    except HAError as err:
        _LOGGER.error("%s", err)
        client.close()
        return 1

    controller = AulaUIController(config, client)
    access = AccessVerifier(config.access_team_domain, config.access_aud,
                            config.access_allowed_emails)
    if not access.enabled:
        _LOGGER.info("Cloudflare Access not configured; public-domain requests are refused")
    server = start_server(controller, access=access)

    def _handle_signal(signum: int, _frame: FrameType | None) -> None:
        _LOGGER.info("Received signal %s", signum)
        controller.stop()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        controller.run_forever()
    finally:
        server.shutdown()
        server.server_close()
        controller.store.save()
        controller.close()
        client.close()
        _LOGGER.info("event=application.stop outcome=complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
