"""Dashboard data layer — pure read/write/format over a SheetsLike.

No Streamlit and no Google imports here on purpose: all I/O goes through the
injected sheets object (SheetsClient in production, a fake in tests), so this
module is fully unit-testable. Updates are idempotent and key-based, and they
MERGE with the existing row so unrelated columns (theme, posted_post_id,
ratings, etc.) are never wiped.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional, Protocol

from src.core.notes import NOTES_SHEET, NOTE_STATUS_NEW
from src.core.queue import (
    JST,
    POST_QUEUE_SHEET,
    STATUS_APPROVED,
    STATUS_DRAFT,
    parse_jst,
    rows_to_dicts,
)
from src.core.tags import join_tags
from src.core.upsert import METRICS_DAILY_SHEET, POSTS_SHEET

logger = logging.getLogger(__name__)


class SheetsLike(Protocol):
    def read_rows(self, sheet: str, a1: str) -> list[list[Any]]: ...

    def append_row(self, sheet: str, row: list[Any]) -> None: ...

    def upsert_row(
        self, sheet: str, key_col: str, key_val: str, row_dict: dict[str, Any]
    ) -> None: ...


def _all(sheets: SheetsLike, sheet: str) -> list[dict[str, Any]]:
    return rows_to_dicts(sheets.read_rows(sheet, "A1:ZZ"))


def _merge_upsert(
    sheets: SheetsLike,
    sheet: str,
    key_col: str,
    key_val: str,
    changes: dict[str, Any],
) -> None:
    """Update a row by key, preserving all columns not in `changes`."""
    existing = next(
        (d for d in _all(sheets, sheet) if str(d.get(key_col, "")) == str(key_val)),
        None,
    )
    if existing is None:
        raise ValueError(f"{key_col}={key_val} が {sheet} に見つかりません")
    merged = {**existing, **changes}
    sheets.upsert_row(sheet, key_col, str(key_val), merged)


def normalize_scheduled_at(value: Any) -> str:
    """Normalize any parsable timestamp to hyphen form 'YYYY-MM-DD HH:MM' (JST).

    Unparsable input is returned trimmed and unchanged (don't destroy what the
    user typed).
    """
    dt = parse_jst(value)
    if dt is None:
        return str(value or "").strip()
    return dt.astimezone(JST).strftime("%Y-%m-%d %H:%M")


def today_jst() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d")


# --- post_queue ---

def read_drafts(sheets: SheetsLike) -> list[dict[str, Any]]:
    drafts = [
        d for d in _all(sheets, POST_QUEUE_SHEET)
        if str(d.get("status", "")).strip() == STATUS_DRAFT
    ]
    drafts.sort(key=lambda d: str(d.get("scheduled_at", "")))
    return drafts


def read_queue_by_statuses(
    sheets: SheetsLike, statuses: list[str]
) -> list[dict[str, Any]]:
    wanted = set(statuses)
    rows = [
        d for d in _all(sheets, POST_QUEUE_SHEET)
        if str(d.get("status", "")).strip() in wanted
    ]
    rows.sort(key=lambda d: str(d.get("scheduled_at", "")))
    return rows


def approve_draft(
    sheets: SheetsLike, queue_id: str, text: str, scheduled_at: str
) -> None:
    """Save edits AND flip the row to approved."""
    _merge_upsert(
        sheets,
        POST_QUEUE_SHEET,
        "queue_id",
        queue_id,
        {
            "text": text,
            "scheduled_at": normalize_scheduled_at(scheduled_at),
            "status": STATUS_APPROVED,
        },
    )


def save_draft(
    sheets: SheetsLike, queue_id: str, text: str, scheduled_at: str
) -> None:
    """Save text/time edits only; keep status=draft."""
    _merge_upsert(
        sheets,
        POST_QUEUE_SHEET,
        "queue_id",
        queue_id,
        {
            "text": text,
            "scheduled_at": normalize_scheduled_at(scheduled_at),
            "status": STATUS_DRAFT,
        },
    )


# --- posts (review / rating) ---

def read_posts(sheets: SheetsLike) -> list[dict[str, Any]]:
    rows = _all(sheets, POSTS_SHEET)
    rows = [r for r in rows if str(r.get("post_id", "")).strip()]
    rows.sort(key=lambda r: str(r.get("posted_at", "")), reverse=True)
    return rows


def set_post_rating(
    sheets: SheetsLike,
    post_id: str,
    rating: str,
    feedback: Optional[str] = None,
) -> None:
    changes: dict[str, Any] = {"rating": rating}
    if feedback is not None:
        changes["feedback"] = feedback
    _merge_upsert(sheets, POSTS_SHEET, "post_id", post_id, changes)


def save_post_feedback(
    sheets: SheetsLike,
    post_id: str,
    tags: list[str],
    rating: str,
    feedback: str,
) -> None:
    """Update a posted item's technique tags + rating + feedback (preserving metrics)."""
    _merge_upsert(
        sheets,
        POSTS_SHEET,
        "post_id",
        post_id,
        {"tags": join_tags(tags), "rating": rating, "feedback": feedback},
    )


def save_queue_feedback(
    sheets: SheetsLike,
    queue_id: str,
    tags: list[str],
    rating: str,
    feedback: str,
) -> None:
    """Update a draft/candidate's tags + rating + feedback (preserving text/status)."""
    _merge_upsert(
        sheets,
        POST_QUEUE_SHEET,
        "queue_id",
        queue_id,
        {"tags": join_tags(tags), "rating": rating, "feedback": feedback},
    )


# --- notes ---

def add_note(sheets: SheetsLike, note_text: str, today: str) -> None:
    """Append a new note (created_at, note, theme, status) with status=new."""
    text = (note_text or "").strip()
    if not text:
        raise ValueError("小言が空です")
    # Column order must match the notes header: created_at, note, theme, status.
    sheets.append_row(NOTES_SHEET, [today, text, "", NOTE_STATUS_NEW])


# --- metrics ---

def read_metrics(sheets: SheetsLike) -> list[dict[str, Any]]:
    rows = [r for r in _all(sheets, METRICS_DAILY_SHEET) if str(r.get("date", "")).strip()]
    rows.sort(key=lambda r: str(r.get("date", "")))
    return rows


def latest_metric(sheets: SheetsLike) -> Optional[dict[str, Any]]:
    rows = read_metrics(sheets)
    return rows[-1] if rows else None


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def metrics_series(sheets: SheetsLike) -> dict[str, list]:
    """Return parallel lists for charting: {dates, followers, views}."""
    rows = read_metrics(sheets)
    return {
        "dates": [str(r.get("date", "")) for r in rows],
        "followers": [_to_int(r.get("followers")) for r in rows],
        "views": [_to_int(r.get("views")) for r in rows],
    }
