"""Logging configuration: plain-text (default) or structured JSON logs.

Kept dependency-free and side-effect-free at import so it is safe to unit test.
``configure_logging`` is called once at app startup (``main.lifespan``); the
JSON format is opt-in via the ``LOG_JSON`` setting so default behavior is
unchanged.
"""

import json
import logging
from typing import Any, Dict

PLAIN_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


class JsonFormatter(logging.Formatter):
    """Render log records as single-line JSON for structured log ingestion."""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO", json_output: bool = False) -> None:
    """Configure the root logger with a single, consistently-formatted handler.

    Replaces existing root handlers so repeated calls (e.g. reloads) do not
    duplicate log lines.
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter() if json_output else logging.Formatter(PLAIN_FORMAT))
    root.handlers = [handler]
