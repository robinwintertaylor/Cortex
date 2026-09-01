"""Structured JSON logging (FR-16). Keys are never redacted here — secrets simply
never reach log calls (keys are hashed at rest and only printed once at creation)."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

_cfg_level = None


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        for key in ("agent", "event_id", "kind", "url", "err"):
            if hasattr(record, key):
                out[key] = getattr(record, key)
        return json.dumps(out, default=str)


def setup_logging(level: str = "INFO") -> None:
    global _cfg_level
    _cfg_level = level
    root = logging.getLogger()
    root.handlers.clear()
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter())
    root.addHandler(h)
    root.setLevel(level.upper())
    # quiet the chatty libs a bit
    for noisy in ("uvicorn.access", "httpx", "httpcore", "asyncpg"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
