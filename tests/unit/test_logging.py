"""Structured log fields stay secret-free and include a bounded traceback."""

from __future__ import annotations

import json
import logging

from qq_ai_bot.logging import JsonFormatter


def test_json_formatter_includes_bounded_traceback() -> None:
    formatter = JsonFormatter()
    try:
        raise KeyError("call_01_missing")
    except KeyError as exc:
        record = logging.LogRecord(
            name="qq_ai_bot.services.processor",
            level=logging.ERROR,
            pathname="processor.py",
            lineno=1,
            msg="turn_internal_failure exception_category=%s",
            args=(type(exc).__name__,),
            exc_info=(type(exc), exc, exc.__traceback__),
        )
    payload = json.loads(formatter.format(record))
    assert payload["exception_category"] == "KeyError"
    assert "call_01_missing" in payload["exception_message"]
    assert "Traceback" in payload["exception_traceback"]
    assert "KeyError" in payload["exception_traceback"]
    assert len(payload["exception_traceback"]) <= 4000
