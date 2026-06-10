"""Centralized logging configuration.

Use this instead of print()/console output so every component logs in a
consistent, timestamped format that is easy to read in GitHub Actions logs.
"""
from __future__ import annotations

import logging
import sys

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure the root logger once and return it.

    Idempotent: calling this repeatedly will not attach duplicate handlers.
    """
    root = logging.getLogger()
    if root.handlers:
        root.setLevel(level)
        return root

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    root.addHandler(handler)
    root.setLevel(level)
    return root
