"""Structured logging.

Configured explicitly by the service entry point, never at import time
(pitfall 15: import-time setup forced debug level and filled the disk).
Console + rotating file, level switchable at runtime.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import time
from pathlib import Path
from typing import Any, Optional

_CONFIGURED = False
_LOG_FILE: Optional[Path] = None
_FILE_HANDLER: Optional[logging.Handler] = None


class JsonFormatter(logging.Formatter):
    """One JSON object per line; extra fields land at the top level."""

    RESERVED = set(
        "name msg args levelname levelno pathname filename module exc_info exc_text "
        "stack_info lineno funcName created msecs relativeCreated thread threadName "
        "processName process taskName message asctime".split()
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
                  + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, val in record.__dict__.items():
            if key in self.RESERVED or key.startswith("_"):
                continue
            try:
                json.dumps(val)
                payload[key] = val
            except (TypeError, ValueError):
                payload[key] = repr(val)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    LEVEL_ICON = {"DEBUG": "·", "INFO": "i", "WARNING": "!", "ERROR": "x", "CRITICAL": "X"}

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)-22s %(message)s", "%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k not in JsonFormatter.RESERVED and not k.startswith("_")
        }
        if extras:
            base += "  " + " ".join(f"{k}={v}" for k, v in extras.items())
        return base


def configure(log_dir: Path, level: str = "info", console: bool = True) -> Path:
    """Install handlers once.  Safe to call again to change the level."""
    global _CONFIGURED, _LOG_FILE, _FILE_HANDLER
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if not _CONFIGURED:
        for h in list(root.handlers):
            root.removeHandler(h)
        if console:
            ch = logging.StreamHandler()
            ch.setFormatter(ConsoleFormatter())
            root.addHandler(ch)
        _LOG_FILE = log_dir / "facw.log"
        fh = logging.handlers.RotatingFileHandler(
            _LOG_FILE, maxBytes=8 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        fh.setFormatter(JsonFormatter())
        root.addHandler(fh)
        _FILE_HANDLER = fh
        # quieten noisy third-party loggers
        for noisy in ("uvicorn.access", "multipart", "PIL", "asyncio"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        root.setLevel(logging.INFO)
        _CONFIGURED = True
    set_level(level)
    # keep uvicorn's loggers on the same handlers
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    return _LOG_FILE or (log_dir / "facw.log")


def set_level(level: str) -> str:
    lvl = {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
    }.get(str(level).lower(), logging.INFO)
    logging.getLogger().setLevel(lvl)
    return logging.getLevelName(lvl)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_file() -> Optional[Path]:
    return _LOG_FILE
