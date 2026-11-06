"""Application logging: console + rotating file ``<log_dir>/app.log``."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from .config import PROJECT_ROOT

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_HANDLER_TAG = "_transferstage_handler"


def setup_logging(
    log_dir: str | Path = "logs",
    level: int = logging.INFO,
    console_level: int | None = None,
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 5,
) -> Path:
    """Configure the root logger. Safe to call more than once (handlers are replaced).

    A relative ``log_dir`` is resolved against the project root. Returns the log file path.
    """
    log_path = Path(log_dir)
    if not log_path.is_absolute():
        log_path = PROJECT_ROOT / log_path
    log_path.mkdir(parents=True, exist_ok=True)
    log_file = log_path / "app.log"

    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, _HANDLER_TAG, False):
            root.removeHandler(h)
            h.close()
    root.setLevel(level)

    formatter = logging.Formatter(LOG_FORMAT)

    console = logging.StreamHandler()
    console.setLevel(console_level if console_level is not None else level)
    console.setFormatter(formatter)
    setattr(console, _HANDLER_TAG, True)
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    setattr(file_handler, _HANDLER_TAG, True)
    root.addHandler(file_handler)

    return log_file
