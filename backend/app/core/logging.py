"""Structured logging helper (ARCHITECTURE §8 "Logging").

Responsibility: emit one-JSON-object-per-line logs to stdout with a
correlation id per request; keep fetch/AI log namespaces separable.

Guarantees enforced here (normative): raw page text, model prompts/outputs
and evidence quotes are NEVER logged verbatim — callers should log hashes
and ``safe_preview`` snippets at most (this module provides ``safe_preview``
so that policy is easy to satisfy).
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")

_RESERVED_RECORD_ATTRS = frozenset({
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "module", "msecs", "message",
    "msg", "name", "pathname", "process", "processName", "relativeCreated",
    "stack_info", "thread", "threadName", "taskName",
})


class JsonFormatter(logging.Formatter):
    """One structured JSON line per record; extra record attrs become fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": correlation_id.get(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED_RECORD_ATTRS and key[0] != "_":
                try:
                    json.dumps(value)
                    payload[key] = value
                except (TypeError, ValueError):
                    payload[key] = repr(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class _Handler(logging.Handler):  # pragma: no cover - trivial passthrough
    pass


def configure_logging(level: int = logging.INFO) -> None:
    """Idempotently attach the JSON handler to the root logger (stdout only)."""
    root = logging.getLogger()
    if getattr(root.handlers[0] if root.handlers else None, "_is_practicesignal_json", False):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler._is_practicesignal_json = True  # type: ignore[attr-defined]
    root.handlers = [handler]
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    """Namespaced logger; modules must not create bare loggers themselves."""
    return logging.getLogger(name)


def safe_preview(text: str, limit: int = 80) -> str:
    """A bounded preview for logs — never the full page text or a quote."""
    flat = " ".join(str(text).split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 3] + "..."
