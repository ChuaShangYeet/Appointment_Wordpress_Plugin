"""Logging: one line per event, as JSON outside development, carrying the request it belongs to, with secrets scrubbed.

* **Format.** ``LOG_FORMAT=json`` writes one JSON object per line (``time``, ``level``, ``logger``, ``message``, ``request_id``, ``tenant_id``, ``exception``
  and any ``extra={"fields": {...}}`` the call adds), which every log collector reads; ``text`` is the readable form for a terminal. ``auto`` (the default) is
  ``text`` in development and tests and ``json`` everywhere else.
* **Correlation.** :data:`request_id_var` and :data:`tenant_id_var` are set per request (``app.core.observability``) and per sweep (``app.worker``), so every
  line written while serving a request - from any module - carries its ``request_id``.
* **Secrets never reach a log line.** :func:`redact` runs over every message and traceback before it is written: bearer tokens, JWTs, our own API keys,
  Google API keys, ``key=...`` / ``token=...`` style query parameters, header values and the password in a database URL. Libraries that print full URLs
  (``httpx`` logs every request, key included) are also turned down to warnings. Redaction is a safety net on top of not logging secrets in the first place.
"""
from __future__ import annotations

import contextvars
import datetime as dt
import json
import logging
import re
import sys
import uuid
from typing import IO, Any

from app.core.config import Settings

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
tenant_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("tenant_id", default=None)

REDACTED = "[redacted]"
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(authorization|x-api-key|x-site-key|x-platform-token|x-signature|x-captcha-token)(\s*[:=]\s*)(?:bearer\s+)?[^\s,;&\"']+"), rf"\1\2{REDACTED}"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), f"Bearer {REDACTED}"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"), REDACTED),  # a JWT
    (re.compile(r"\b(?:pk|sk)_[A-Za-z0-9_-]{20,}"), REDACTED),  # our API keys
    (re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"), REDACTED),  # a Google API key
    (re.compile(r"(?i)\b(api[_-]?key|key|token|secret|password|passwd|signature|access_token|auth_token)=([^&\s\"'<>]+)"), rf"\1={REDACTED}"),
    (re.compile(r"(?<=://)([^:/\s@]+):([^@\s/]+)@"), rf"\1:{REDACTED}@"),  # the password in postgresql://user:password@host
)

#: Loggers that print what they are given in full (``httpx`` logs each request URL, API key and all): kept at warnings.
QUIET_LOGGERS = ("httpx", "httpcore")


def redact(text: str) -> str:
    """``text`` with every secret it contains replaced by ``[redacted]``."""
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def bind_tenant(tenant_id: uuid.UUID | str | None) -> None:
    """Mark the rest of this request's log lines as the business's."""
    tenant_id_var.set(str(tenant_id) if tenant_id is not None else None)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {}
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            entry.update({str(k): redact(v) if isinstance(v, str) else v for k, v in fields.items()})
        entry.update({
            "time": dt.datetime.fromtimestamp(record.created, dt.UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": redact(record.getMessage()),
        })
        for name, var in (("request_id", request_id_var), ("tenant_id", tenant_id_var)):
            if (value := var.get()) is not None:
                entry[name] = value
        if record.exc_info:
            entry["exception"] = redact(self.formatException(record.exc_info))
        if record.stack_info:
            entry["stack"] = redact(self.formatStack(record.stack_info))
        return json.dumps(entry, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        request_id = request_id_var.get()
        return redact(f"{line} [{request_id}]" if request_id else line)


HANDLER_NAME = "app-log"


def resolve_format(settings: Settings) -> str:
    if settings.log_format != "auto":
        return settings.log_format
    return "text" if settings.environment in ("development", "test") else "json"


def configure_logging(level: str = "INFO", fmt: str = "json", *, stream: IO[str] | None = None) -> logging.Handler:
    """Send every log line, ours and the libraries', through one handler on standard output. Safe to call again (the handler is replaced, not added)."""
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.set_name(HANDLER_NAME)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if h.get_name() != HANDLER_NAME] + [handler]
    root.setLevel(level.upper())
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    # uvicorn would print its own, differently shaped lines: its errors go through ours, its access line is replaced by the request log
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    return handler


def unconfigure_logging() -> None:
    """Remove the handler :func:`configure_logging` installed (tests)."""
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if h.get_name() != HANDLER_NAME]
