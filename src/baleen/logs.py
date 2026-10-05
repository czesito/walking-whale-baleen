"""Logging (SEC-11): data/logs/baleen.log, rotating 5 x 5 MB. Local only; may contain names in paths."""

from __future__ import annotations

import logging
import logging.handlers
import sys

from .home import Home

_configured = False


def setup(home: Home, *, console: bool = True, level: int = logging.INFO) -> str:
    global _configured
    home.logs_dir.mkdir(parents=True, exist_ok=True)
    path = home.logs_dir / "baleen.log"
    root = logging.getLogger("baleen")
    if _configured:
        return str(path)
    root.setLevel(level)
    fh = logging.handlers.RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=4, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(fh)
    if console:
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
        ch.setLevel(logging.WARNING)
        root.addHandler(ch)
    _configured = True
    return str(path)
