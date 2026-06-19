"""Shared definitions and helpers for the post_queue sheet (Phase 2).

Single source of truth for the sheet name, header order, status values, and
the defensive date parsing used by both generate_drafts and publish_queue.
Pure stdlib so init_sheets can import it without heavy dependencies.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

JST = timezone(timedelta(hours=9))

POST_QUEUE_SHEET = "post_queue"
POST_QUEUE_HEADER = [
    "queue_id",
    "scheduled_at",
    "text",
    "theme",
    "status",
    "posted_post_id",
    "posted_at",  # actual publish time (JST); recorded on success
    "tags",  # technique tags, " | "-joined (human feedback)
    "rating",  # good / ok / bad (human feedback)
    "feedback",  # free-text note (human feedback)
    "thread_id",  # groups a thread (連投); empty = standalone single post
    "seq",  # position in the thread: 0 = parent, 1.. = replies in order
]

# status values
STATUS_DRAFT = "draft"
STATUS_APPROVED = "approved"
STATUS_POSTED = "posted"
STATUS_FAILED = "failed"

# A row is only ever published when status == approved. These are terminal and
# must never be reprocessed.
TERMINAL_STATUSES = {STATUS_POSTED, STATUS_FAILED}


def rows_to_dicts(rows: list[list[Any]]) -> list[dict[str, Any]]:
    """Turn a [header, *data] grid into a list of header-keyed dicts.

    Missing trailing cells default to "" so callers never IndexError.
    """
    if not rows:
        return []
    header = [str(c) for c in rows[0]]
    out: list[dict[str, Any]] = []
    for row in rows[1:]:
        out.append(
            {col: (row[i] if i < len(row) else "") for i, col in enumerate(header)}
        )
    return out


def parse_jst(value: Any) -> datetime | None:
    """Parse a scheduled_at value into a timezone-aware datetime (JST default).

    Defensive about format drift: tries ISO 8601 first, then a few common
    explicit formats. A naive datetime is assumed to be JST. Returns None when
    the value cannot be parsed at all.
    """
    if not value:
        return None
    text = str(value).strip()
    dt: datetime | None = None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in (
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
        ):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    return dt
