"""Idempotent upsert logic for the metrics_daily sheet.

`upsert_daily` writes one row per date (re-running the same day overwrites,
never duplicates) and fills in follower_delta from the previous day's
followers when not supplied.
"""
from __future__ import annotations

import logging
from typing import Protocol

from src.core.models import DailyMetric

logger = logging.getLogger(__name__)

METRICS_DAILY_SHEET = "metrics_daily"
POSTS_SHEET = "posts"


class SheetsLike(Protocol):
    """The minimal Sheets surface upsert_daily depends on (eases testing)."""

    def read_rows(self, sheet: str, a1: str) -> list[list[object]]: ...

    def upsert_row(
        self, sheet: str, key_col: str, key_val: str, row_dict: dict[str, object]
    ) -> None: ...


def compute_follower_delta(sheets: SheetsLike, metric: DailyMetric) -> int | None:
    """Delta vs the most recent earlier day's followers, or None if unknown."""
    rows = sheets.read_rows(METRICS_DAILY_SHEET, "A1:ZZ")
    if not rows:
        return None

    header = rows[0]
    try:
        date_idx = header.index("date")
        followers_idx = header.index("followers")
    except ValueError:
        logger.warning("metrics_daily header missing date/followers: %s", header)
        return None

    prev_followers: int | None = None
    prev_date = ""
    for row in rows[1:]:
        if len(row) <= max(date_idx, followers_idx):
            continue
        row_date = str(row[date_idx])
        # ISO dates sort lexicographically, so ">= prev_date" finds the latest
        # day strictly before today.
        if row_date and row_date < metric.date and row_date >= prev_date:
            try:
                prev_followers = int(row[followers_idx])
                prev_date = row_date
            except (TypeError, ValueError):
                continue

    if prev_followers is None:
        return None
    return metric.followers - prev_followers


def upsert_daily(sheets: SheetsLike, metric: DailyMetric) -> DailyMetric:
    """Write a daily metric keyed by date, idempotently.

    Returns the metric actually written (with follower_delta resolved).
    """
    delta = metric.follower_delta
    if delta is None:
        delta = compute_follower_delta(sheets, metric)

    final = DailyMetric(
        date=metric.date,
        followers=metric.followers,
        views=metric.views,
        follower_delta=delta,
        note=metric.note,
    )

    sheets.upsert_row(
        METRICS_DAILY_SHEET,
        key_col="date",
        key_val=final.date,
        row_dict={
            "date": final.date,
            "followers": final.followers,
            "views": final.views,
            "follower_delta": "" if final.follower_delta is None else final.follower_delta,
            "note": final.note,
        },
    )
    return final
