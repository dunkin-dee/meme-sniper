"""Logging configuration.

The firehose is high volume, so per-event logging is deliberately avoided in
favour of periodic heartbeats. Anything logged at INFO should be something a
human would actually want to see once a minute.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path


def setup(level: str = "INFO", log_file: str | Path | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)

    # websockets logs every frame at DEBUG; never useful here.
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
