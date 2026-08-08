"""
Structured JSON logging with a correlation id that follows an event end to end.

Stdlib logging with a custom formatter rather than structlog: it's one less
dependency, and uvicorn/httpx already log through stdlib, so this way *their*
records come out as JSON too instead of a second, differently-shaped log stream.

The correlation id lives in a ContextVar so it propagates through async call
stacks without being threaded through every function signature, and stays
correct when the worker processes a batch concurrently.
"""

import json
import logging
import os
import sys
import uuid
from contextvars import ContextVar
from typing import Any

CORRELATION_ID: ContextVar[str | None] = ContextVar("correlation_id", default=None)

# Attributes LogRecord always carries; anything else was passed via `extra=` and
# is worth emitting.
_RESERVED = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "msecs",
    "message",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
    "taskName",
}


def new_correlation_id() -> str:
    return uuid.uuid4().hex[:16]


def set_correlation_id(value: str | None) -> str:
    cid = value or new_correlation_id()
    CORRELATION_ID.set(cid)
    return cid


def get_correlation_id() -> str | None:
    return CORRELATION_ID.get()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }

        cid = CORRELATION_ID.get()
        if cid:
            payload["correlation_id"] = cid

        # Anything passed as extra={...} -- message_id, event_type, status, etc.
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    """Human-readable output for local development, where JSON is just noise."""

    def format(self, record: logging.LogRecord) -> str:
        extras = " ".join(
            f"{k}={v}"
            for k, v in record.__dict__.items()
            if k not in _RESERVED and not k.startswith("_")
        )
        cid = CORRELATION_ID.get()
        prefix = f"[{cid}] " if cid else ""
        base = f"{record.levelname:<8} {prefix}{record.getMessage()}"
        line = f"{base} {extras}".rstrip()
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def configure_logging() -> None:
    """Idempotent: safe to call from both the API lifespan and the worker."""
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    fmt = os.getenv("LOG_FORMAT", "json").lower()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(ConsoleFormatter() if fmt == "console" else JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # uvicorn installs its own handlers; clearing them makes it propagate to ours
    # so every line in the container's stdout has the same shape.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True

    # httpx logs every request at INFO, which duplicates our own delivery logs.
    logging.getLogger("httpx").setLevel("WARNING")


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
