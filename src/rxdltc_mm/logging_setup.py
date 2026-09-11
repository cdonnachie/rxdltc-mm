"""Structured logging with secret redaction.

Every record passes through :class:`RedactingFilter`, which replaces any
registered secret value with ``***``. The KDF transport additionally never
includes the ``userpass`` field in anything it logs, so redaction here is a
second line of defence rather than the only one.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

_SECRETS: list[str] = []


def register_secret(value: str | None) -> None:
    if value and value not in _SECRETS and len(value) >= 4:
        _SECRETS.append(value)


def redact(text: str) -> str:
    for s in _SECRETS:
        if s in text:
            text = text.replace(s, "***")
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            msg = str(record.msg)
        record.msg = redact(msg)
        record.args = ()
        extra = getattr(record, "extra", None)
        if isinstance(extra, dict):
            record.extra = json.loads(redact(json.dumps(extra, default=str)))
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "extra", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(record.created))
        line = f"{ts} {record.levelname:<7} {record.name}: {record.getMessage()}"
        extra = getattr(record, "extra", None)
        if isinstance(extra, dict) and extra:
            line += " " + " ".join(f"{k}={_fmt(v)}" for k, v in extra.items())
        if record.exc_info:
            line += "\n" + redact(self.formatException(record.exc_info))
        return line


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:.6g}"
    s = str(v)
    return s if " " not in s else json.dumps(s)


class BotLogger(logging.LoggerAdapter):
    """``log.info("msg", key=value)`` -> structured ``extra`` fields."""

    def process(self, msg: str, kwargs: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        std = {k: kwargs.pop(k) for k in ("exc_info", "stack_info", "stacklevel") if k in kwargs}
        extra = dict(self.extra or {})
        extra.update(kwargs)
        kwargs.clear()
        kwargs.update(std)
        kwargs["extra"] = {"extra": extra}
        return msg, kwargs


def get_logger(name: str) -> BotLogger:
    return BotLogger(logging.getLogger(name), {})


def setup_logging(level: str = "INFO", fmt: str = "text") -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    handler.addFilter(RedactingFilter())
    root.addHandler(handler)
    root.setLevel(level.upper())
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
