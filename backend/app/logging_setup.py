import json
import logging
import os
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler

from .config import settings

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
user_id_var: ContextVar[str] = ContextVar("user_id", default="-")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
            "request_id": request_id_var.get(),
            "user_id": user_id_var.get(),
        }
        payload.update(getattr(record, "ctx", {}))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging() -> None:
    root = logging.getLogger()
    if getattr(root, "_apollo_configured", False):
        return
    root.setLevel(settings.log_level)
    fmt = JsonFormatter()
    os.makedirs(os.path.dirname(settings.log_file) or ".", exist_ok=True)
    for h in (logging.StreamHandler(), RotatingFileHandler(settings.log_file, maxBytes=5_000_000, backupCount=3)):
        h.setFormatter(fmt)
        root.addHandler(h)
    root._apollo_configured = True  # type: ignore[attr-defined]


def log_event(event: str, level: int = logging.INFO, **ctx) -> None:
    """Never pass passwords, tokens, API keys or patient details in ctx."""
    logging.getLogger("apollocare").log(level, event, extra={"ctx": ctx})
