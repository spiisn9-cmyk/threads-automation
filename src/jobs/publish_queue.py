"""F5: publish approved, due posts from the post_queue sheet — with anti-ban guards.

This tool posts via the official Threads API (threads_content_publish). To keep
a human-like, conservative pace and avoid the bursts/odd-hour posting that get
accounts frozen, every run applies these guards in order and publishes AT MOST
one post:

  1. Time-window guard — only post within [START, END) hour (JST).
  2. Daily cap        — stop if today's posted count >= MAX_POSTS_PER_DAY.
  3. Min interval     — stop if the last post was < MIN_HOURS_BETWEEN_POSTS ago.
  4. Per-run cap      — publish only the earliest due row (MAX_POSTS_PER_RUN);
                        the rest carry over to later runs.
  5. Jitter           — sleep a random 0..POST_JITTER_MINUTES before posting so
                        the publish minute isn't a regular ":05".

Only status==approved AND scheduled_at<=now(JST) rows are candidates. Drafts are
NEVER published. posted/failed rows are terminal and never retried, so repeated
runs cannot double-post. Every skip reason is written to the logs sheet.

Run from the repo root:
    python -m src.jobs.publish_queue
"""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Optional, Protocol

from config.settings import (
    JST,
    MAX_POSTS_PER_DAY,
    MAX_POSTS_PER_RUN,
    MIN_HOURS_BETWEEN_POSTS,
    POST_JITTER_MINUTES,
    POST_WINDOW_END_HOUR,
    POST_WINDOW_START_HOUR,
    load_settings,
)
from src.core.queue import (
    POST_QUEUE_SHEET,
    STATUS_APPROVED,
    STATUS_FAILED,
    STATUS_POSTED,
    parse_jst,
    rows_to_dicts,
)
from src.utils.logging_setup import setup_logging

logger = logging.getLogger("publish_queue")

JOB_NAME = "publish_queue"
LOGS_SHEET = "logs"

LogFn = Callable[[str, int, str], None]


class SheetsLike(Protocol):
    def read_rows(self, sheet: str, a1: str) -> list[list[Any]]: ...

    def upsert_row(
        self, sheet: str, key_col: str, key_val: str, row_dict: dict[str, Any]
    ) -> None: ...

    def append_row(self, sheet: str, row: list[Any]) -> None: ...


class ThreadsLike(Protocol):
    def create_post(self, text: str) -> str: ...

    def publish_post(self, creation_id: str) -> str: ...


def _is_due(row: dict[str, Any], now: datetime) -> bool:
    """True only when status == approved and scheduled_at is in the past."""
    if str(row.get("status", "")).strip() != STATUS_APPROVED:
        return False
    scheduled = parse_jst(row.get("scheduled_at"))
    if scheduled is None:
        logger.warning(
            "Skipping queue_id=%s: unparseable scheduled_at=%r",
            row.get("queue_id"),
            row.get("scheduled_at"),
        )
        return False
    return scheduled <= now


def select_due(rows: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Approved + due rows, sorted by scheduled_at (earliest first)."""
    due = [r for r in rows if _is_due(r, now)]
    due.sort(key=lambda r: str(r.get("scheduled_at", "")))
    return due


def _posted_at_times(rows: list[dict[str, Any]]) -> list[datetime]:
    """Parsed posted_at timestamps of all already-posted rows."""
    out: list[datetime] = []
    for r in rows:
        if str(r.get("status", "")).strip() != STATUS_POSTED:
            continue
        dt = parse_jst(r.get("posted_at"))
        if dt is not None:
            out.append(dt.astimezone(JST))
    return out


def _count_posted_today(rows: list[dict[str, Any]], now: datetime) -> int:
    today = now.astimezone(JST).date()
    return sum(1 for dt in _posted_at_times(rows) if dt.date() == today)


def _last_posted_at(rows: list[dict[str, Any]]) -> Optional[datetime]:
    times = _posted_at_times(rows)
    return max(times) if times else None


def _update_status(
    sheets: SheetsLike,
    row: dict[str, Any],
    status: str,
    posted_post_id: str = "",
    posted_at: str = "",
) -> None:
    new_row = {
        **row,
        "status": status,
        "posted_post_id": posted_post_id,
        "posted_at": posted_at or row.get("posted_at", ""),
    }
    sheets.upsert_row(
        POST_QUEUE_SHEET,
        key_col="queue_id",
        key_val=str(row.get("queue_id")),
        row_dict=new_row,
    )


def publish_due(
    sheets: SheetsLike,
    threads: ThreadsLike,
    now: datetime,
    *,
    log_fn: Optional[LogFn] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    jitter_fn: Optional[Callable[[], float]] = None,
    max_per_run: int = MAX_POSTS_PER_RUN,
    max_per_day: int = MAX_POSTS_PER_DAY,
    min_hours: float = MIN_HOURS_BETWEEN_POSTS,
    window_start: int = POST_WINDOW_START_HOUR,
    window_end: int = POST_WINDOW_END_HOUR,
) -> dict[str, int]:
    """Apply safety guards and publish at most `max_per_run` due posts.

    Returns counts: {"due", "posted", "failed"}.
    """
    if jitter_fn is None:
        jitter_fn = lambda: random.uniform(0, POST_JITTER_MINUTES * 60)

    def skip(reason: str) -> None:
        logger.info("Publish skipped: %s", reason)
        if log_fn is not None:
            log_fn("skipped", 0, reason)

    now_jst = now.astimezone(JST)
    rows = rows_to_dicts(sheets.read_rows(POST_QUEUE_SHEET, "A1:ZZ"))
    due = select_due(rows, now)
    base = {"due": len(due), "posted": 0, "failed": 0}

    # Guard 1: time window (JST).
    if not (window_start <= now_jst.hour < window_end):
        skip(
            f"時間帯外スキップ: now={now_jst.strftime('%H:%M')} "
            f"許可={window_start:02d}:00-{window_end:02d}:00(JST)"
        )
        return base

    # Guard 2: daily cap.
    posted_today = _count_posted_today(rows, now)
    if posted_today >= max_per_day:
        skip(f"日次上限スキップ: 本日{posted_today}件 / 上限{max_per_day}件")
        return base

    # Guard 3: minimum interval since the last successful post.
    last = _last_posted_at(rows)
    if last is not None:
        gap = now_jst - last
        if gap < timedelta(hours=min_hours):
            hrs = gap.total_seconds() / 3600
            skip(
                f"最小間隔スキップ: 前回投稿から{hrs:.1f}h < {min_hours}h"
            )
            return base

    # Guard 4: per-run cap — publish only the earliest due row(s).
    targets = due[:max_per_run]
    if not targets:
        logger.info("No due posts to publish")
        return base

    posted = 0
    failed = 0
    for row in targets:
        queue_id = str(row.get("queue_id"))
        text = str(row.get("text", ""))

        # Guard 5: human-like jitter right before posting.
        delay = max(0.0, float(jitter_fn()))
        if delay:
            logger.info("Jitter: sleeping %.0fs before publishing %s", delay, queue_id)
            sleep_fn(delay)

        try:
            creation_id = threads.create_post(text)
            media_id = threads.publish_post(creation_id)
            posted_at = now_jst.strftime("%Y-%m-%dT%H:%M:%S%z")
            _update_status(
                sheets, row, STATUS_POSTED, posted_post_id=media_id, posted_at=posted_at
            )
            posted += 1
            logger.info("Published queue_id=%s -> media_id=%s", queue_id, media_id)
        except Exception as exc:
            logger.error("Publish failed for queue_id=%s: %s", queue_id, exc)
            try:
                _update_status(sheets, row, STATUS_FAILED)
            except Exception as upd_exc:
                logger.error("Could not mark queue_id=%s failed: %s", queue_id, upd_exc)
            if log_fn is not None:
                log_fn("failed", 1, f"queue_id={queue_id}: {exc}")
            failed += 1

    return {"due": len(due), "posted": posted, "failed": failed}


def _record_log(sheets: SheetsLike, status: str, count: int, message: str) -> None:
    now_iso = datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z")
    sheets.append_row(LOGS_SHEET, [now_iso, JOB_NAME, status, count, message])


def run() -> int:
    setup_logging()
    # Imported lazily so the testable core (publish_due/select_due) doesn't
    # require the httpx / google SDKs to be installed.
    from src.clients.sheets_client import SheetsClient
    from src.clients.threads_client import ThreadsClient

    try:
        settings = load_settings()
    except Exception as exc:
        logger.error("Failed to load settings: %s", exc)
        return 1

    try:
        sheets = SheetsClient(settings.google_sa_json, settings.spreadsheet_id)
    except Exception as exc:
        logger.error("Sheets init failed: %s", exc)
        return 1

    def log_fn(status: str, count: int, message: str) -> None:
        try:
            _record_log(sheets, status, count, message)
        except Exception as exc:
            logger.error("logs write failed: %s", exc)

    try:
        with ThreadsClient(settings.threads_access_token, user_id="me") as threads:
            result = publish_due(sheets, threads, datetime.now(JST), log_fn=log_fn)
    except Exception as exc:
        logger.error("publish_queue run failed: %s", exc)
        log_fn("failed", 0, str(exc))
        return 1

    log_fn(
        "ok",
        result["posted"],
        f"due={result['due']} posted={result['posted']} failed={result['failed']}",
    )
    logger.info("publish_queue finished: %s", result)
    return 0 if result["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(run())
