"""Tests for the structured logging helpers."""

import json
import logging

from app.logging_config import JsonFormatter, PLAIN_FORMAT, configure_logging


class TestJsonFormatter:
    def _record(self, msg="hello", level=logging.INFO):
        return logging.LogRecord(
            name="fadeout.test",
            level=level,
            pathname=__file__,
            lineno=1,
            msg=msg,
            args=(),
            exc_info=None,
        )

    def test_emits_valid_json(self):
        out = JsonFormatter().format(self._record("processing mix"))
        parsed = json.loads(out)
        assert parsed["msg"] == "processing mix"
        assert parsed["level"] == "INFO"
        assert parsed["logger"] == "fadeout.test"
        assert "ts" in parsed

    def test_formats_args(self):
        rec = logging.LogRecord(
            name="fadeout", level=logging.WARNING, pathname=__file__,
            lineno=1, msg="count=%d", args=(3,), exc_info=None,
        )
        parsed = json.loads(JsonFormatter().format(rec))
        assert parsed["msg"] == "count=3"
        assert parsed["level"] == "WARNING"


class TestConfigureLogging:
    def test_plain_single_handler(self):
        configure_logging("DEBUG", json_output=False)
        root = logging.getLogger()
        assert len(root.handlers) == 1
        assert root.level == logging.DEBUG
        assert not isinstance(root.handlers[0].formatter, JsonFormatter)

    def test_json_handler(self):
        configure_logging("INFO", json_output=True)
        root = logging.getLogger()
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, JsonFormatter)

    def test_no_duplicate_handlers_on_repeat(self):
        configure_logging("INFO", json_output=False)
        configure_logging("INFO", json_output=False)
        assert len(logging.getLogger().handlers) == 1

    def test_plain_format_constant(self):
        assert "%(message)s" in PLAIN_FORMAT
