"""Optional, stderr-first logging for Atlas programs.

This module does not write execution history, manage log files, or configure the
root logger. Standard-library loggers and handlers remain usable independently.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TextIO

__all__ = ["configure_logging", "get_logger", "log_event"]

_CONTEXT = (
    ("host_id", "ATLAS_HOST_ID"),
    ("program", "ATLAS_PROGRAM_NAME"),
    ("command", "ATLAS_COMMAND_NAME"),
    ("run_id", "ATLAS_RUN_ID"),
    ("parent_run_id", "ATLAS_PARENT_RUN_ID"),
    ("operation_id", "ATLAS_OPERATION_ID"),
)
_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


class _AtlasHandler(logging.StreamHandler):
    """Identifies handlers owned by configure_logging for safe reconfiguration."""


class _AtlasFormatter(logging.Formatter):
    def __init__(self, output_format: str) -> None:
        super().__init__()
        self.output_format = output_format

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, UTC).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
        payload: dict[str, object] = {
            "schema": "atlas.application-log/v1",
            "timestamp": timestamp,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field, variable in _CONTEXT:
            value = os.environ.get(variable)
            if value:
                payload[field] = value
        event = getattr(record, "atlas_event", None)
        if event is not None:
            payload["event"] = event
        fields = getattr(record, "atlas_fields", None)
        if fields:
            payload["fields"] = fields
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if self.output_format == "json":
            return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

        message = str(payload["message"]).replace("\r", "\\r").replace("\n", "\\n")
        parts = [f"{timestamp} {record.levelname} {record.name}: {message}"]
        if event is not None:
            parts.append(f"event={json.dumps(event, ensure_ascii=False)}")
        if fields:
            parts.extend(
                f"{key}={json.dumps(value, ensure_ascii=False)}"
                for key, value in sorted(fields.items())
            )
        for key in ("run_id", "operation_id", "host_id"):
            if key in payload:
                parts.append(f"{key}={json.dumps(payload[key], ensure_ascii=False)}")
        if "exception" in payload:
            parts.append(f"exception={json.dumps(payload['exception'], ensure_ascii=False)}")
        return " ".join(parts)


def get_logger(name: str) -> logging.Logger:
    """Return an ordinary stdlib logger (configuration is explicit)."""
    return logging.getLogger(name)


def configure_logging(
    name: str,
    *,
    level: str | int = "INFO",
    format: str = "text",
    stream: TextIO | None = None,
) -> logging.Logger:
    """Set up one named logger for stderr text or JSON Lines output.

    Repeated calls replace only the handler installed by this helper. The root
    logger and unrelated handlers are never modified. Child loggers inherit the
    configured parent's behavior through normal logging propagation.
    """
    if not isinstance(name, str) or name == "root" or not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_.-]*", name
    ):
        raise ValueError("a valid non-root logger name is required")
    if not isinstance(format, str) or format not in {"text", "json"}:
        raise ValueError("log format must be text or json")
    if isinstance(level, str):
        selected_level = _LEVELS.get(level.upper())
        if selected_level is None:
            raise ValueError("invalid logging level")
    elif type(level) is int and 0 <= level <= logging.CRITICAL:
        selected_level = level
    else:
        raise ValueError("invalid logging level")

    logger = logging.getLogger(name)
    for handler in list(logger.handlers):
        if isinstance(handler, _AtlasHandler):
            logger.removeHandler(handler)
            handler.close()
    handler = _AtlasHandler(sys.stderr if stream is None else stream)
    handler.setFormatter(_AtlasFormatter(format))
    logger.addHandler(handler)
    logger.setLevel(selected_level)
    logger.propagate = False
    return logger


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    message: str | None = None,
    fields: Mapping[str, str | int | float | bool | None] | None = None,
) -> None:
    """Log a named event with optional JSON-scalar, explicitly supplied fields.

    Callers are responsible for excluding secrets and sensitive data. No
    blanket redaction or arbitrary-object serialization is attempted.
    """
    if not isinstance(event, str) or not re.fullmatch(
        r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*", event
    ):
        raise ValueError("invalid log event name")
    if fields is not None and not isinstance(fields, Mapping):
        raise ValueError("log fields must be a mapping")
    values = {} if fields is None else dict(fields)
    for key, value in values.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", key):
            raise ValueError("invalid log field name")
        if value is not None and type(value) not in (str, int, float, bool):
            raise ValueError("log fields must contain JSON scalar values")
        if type(value) is float and not math.isfinite(value):
            raise ValueError("log fields must contain finite numbers")
    logger.log(
        level,
        event if message is None else message,
        extra={"atlas_event": event, "atlas_fields": values},
    )
