"""Structured logging, plus the per-case reasoning trace.

Two things happen here:

1. Ordinary application logs go out as JSON lines, which is what Cloud Logging
   wants and what makes a soak run greppable afterwards.
2. Every decision the agents make about a case is *also* recorded as a
   ``TrailEvent`` in the database. That trail is the audit story: an agency
   receiving one of our reports could ask why, and the answer is on the record.
   ``trace()`` is the logging half of that; the persistence half lives in the
   repository.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

_CONFIGURED = False

# Attributes LogRecord always carries; anything else was passed via `extra`
# and belongs in the structured payload.
_STANDARD_ATTRS = frozenset(
    """args asctime created exc_info exc_text filename funcName levelname levelno
    lineno module msecs message msg name pathname process processName relativeCreated
    stack_info thread threadName taskName""".split()
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            # Cloud Logging picks up "severity"; everything else ignores it harmlessly.
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    """Human-readable format for local runs, where JSON lines are just noise."""

    LEVEL_COLORS = {
        "DEBUG": "\033[38;5;245m",
        "INFO": "\033[38;5;33m",
        "WARNING": "\033[38;5;214m",
        "ERROR": "\033[38;5;196m",
        "CRITICAL": "\033[48;5;196m\033[97m",
    }
    RESET = "\033[0m"
    DIM = "\033[38;5;245m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.LEVEL_COLORS.get(record.levelname, "")
        stamp = datetime.fromtimestamp(record.created, tz=UTC).strftime("%H:%M:%S")
        head = f"{self.DIM}{stamp}{self.RESET} {color}{record.levelname:<7}{self.RESET}"
        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k not in _STANDARD_ATTRS and not k.startswith("_")
        }
        tail = ""
        if extras:
            rendered = " ".join(f"{k}={v}" for k, v in extras.items())
            tail = f" {self.DIM}{rendered}{self.RESET}"
        line = f"{head} {record.getMessage()}{tail}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def configure_logging(level: str = "INFO", *, json_output: bool = False) -> None:
    """Install the root handler. Idempotent, so CLI and web can both call it."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if json_output else ConsoleFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    # These are chatty and never tell us anything we want during a soak run.
    for noisy in ("httpx", "httpcore", "urllib3", "google", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def trace(logger: logging.Logger, case_id: str, stage: str, message: str, **fields: Any) -> None:
    """Log one step of an agent's reasoning about a specific case.

    Mirrors what gets written to the case's trail, so the log and the dashboard
    tell the same story.
    """
    logger.info(message, extra={"case_id": case_id, "stage": stage, **fields})
