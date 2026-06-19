"""F5: publish approved, due posts from the post_queue sheet — with anti-ban guards.

This tool posts via the official Threads API (threads_content_publish). To keep
a human-like, conservative pace and avoid the bursts/odd-hour posting that get
accounts frozen, every run applies these guards in order and publishes AT MOST
one post:

  1. Time-window guard — only post within [START, END) hour (JST).
  2. Daily cap        — stop if today's posted count >= MAX_POSTS_PER_DAY.
  3. Min interval     — stop if the last post was < MIN_HOURS_BETWEEN_POSTS ago.
  4. Per-run cap      — publish up to MAX_POSTS_PER_RUN UNITS; the rest carry
                        over. A thread (parent + replies) is ONE unit: it
                        publishes whole in one run (parent → replies in seq
                        order, replies reply_to the parent) and consumes one
                        daily-cap / per-run slot.
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
    PUBLISH_STATUS_MAX_CHECKS,
    PUBLISH_STATUS_POLL_SECONDS,
    THREAD_REPLY_DELAY_SECONDS,
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
    def create_post(self, text: str, reply_to_id: Optional[str] = None) -> str: ...

    def get_container_status(self, creation_id: str) -> dict[str, str]: ...

    def publish_post(self, creation_id: str) -> str: ...


def _seq_of(row: dict[str, Any]) -> int:
    try:
        return int(row.get("seq") or 0)
    except (TypeError, ValueError):
        return 0


def _is_head(row: dict[str, Any]) -> bool:
    """A 'unit head': a standalone post (no thread_id) or a thread parent (seq 0).

    A thread + its replies counts as ONE publish unit; only the head consumes a
    daily-cap / per-run slot. Replies (seq>=1) do not.
    """
    thread_id = str(row.get("thread_id", "")).strip()
    return (not thread_id) or _seq_of(row) == 0


# Container statuses that mean "stop waiting".
_STATUS_FINISHED = "FINISHED"
_STATUS_FAILURES = {"ERROR", "EXPIRED"}


def wait_until_finished(
    threads: ThreadsLike,
    creation_id: str,
    *,
    sleep_fn,
    poll_seconds: float,
    max_checks: int,
) -> None:
    """Poll the container until status=FINISHED before publishing.

    Raises RuntimeError on ERROR/EXPIRED or on timeout. Logs each check and the
    final status so the wait is visible in the run logs.
    """
    last_status = ""
    for attempt in range(1, max_checks + 1):
        info = threads.get_container_status(creation_id)
        status = str(info.get("status") or "").upper()
        last_status = status or last_status
        if status == _STATUS_FINISHED:
            logger.info("Container %s FINISHED after %d check(s)", creation_id, attempt)
            return
        if status in _STATUS_FAILURES:
            raise RuntimeError(
                f"container {status}: {info.get('error_message') or ''} "
                f"(creation_id={creation_id}, checks={attempt})"
            )
        logger.info(
            "Container %s status=%s (check %d/%d); waiting %.0fs",
            creation_id,
            status or "?",
            attempt,
            max_checks,
            poll_seconds,
        )
        if attempt < max_checks:
            sleep_fn(poll_seconds)
    raise RuntimeError(
        f"container not FINISHED after {max_checks} checks "
        f"(last status={last_status or '?'}, creation_id={creation_id})"
    )


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
    """Posted UNITS today (a thread counts once; replies don't consume the cap)."""
    today = now.astimezone(JST).date()
    n = 0
    for r in rows:
        if str(r.get("status", "")).strip() != STATUS_POSTED:
            continue
        if not _is_head(r):
            continue  # replies don't consume the daily cap
        dt = parse_jst(r.get("posted_at"))
        if dt is not None and dt.astimezone(JST).date() == today:
            n += 1
    return n


def _last_posted_at(rows: list[dict[str, Any]]) -> Optional[datetime]:
    times = _posted_at_times(rows)
    return max(times) if times else None


def publish_thread(
    threads: ThreadsLike,
    texts: list[str],
    *,
    sleep_fn: Callable[[float], None],
    wait_fn: Callable[[str], None],
    delay_seconds: float,
    on_published: Optional[Callable[[int, str], None]] = None,
) -> list[str]:
    """Publish a parent + replies as one thread; return media_ids in order.

    texts[0] is the parent (posted with no reply target); every reply is posted
    with reply_to_id = the PARENT's media_id (per the Threads spec for 連投).
    Each post waits until its container is FINISHED before publishing, with a
    short delay between posts. `on_published(idx, media_id)` fires right after
    each successful publish (used to mark the sheet row), so a mid-thread failure
    leaves already-posted parts recorded.
    """
    media_ids: list[str] = []
    parent_media: Optional[str] = None
    for idx, text in enumerate(texts):
        reply_to = parent_media  # None for the parent; parent id for every reply
        creation_id = threads.create_post(text, reply_to_id=reply_to)
        wait_fn(creation_id)
        media_id = threads.publish_post(creation_id)
        media_ids.append(media_id)
        if on_published is not None:
            on_published(idx, media_id)
        if idx == 0:
            parent_media = media_id
        if delay_seconds and idx < len(texts) - 1:
            sleep_fn(delay_seconds)
    return media_ids


def _approved_thread_chain(
    all_rows: list[dict[str, Any]], thread_id: str
) -> list[dict[str, Any]]:
    """Contiguous approved rows of a thread starting at the parent (seq 0)."""
    in_thread = sorted(
        (r for r in all_rows if str(r.get("thread_id", "")).strip() == thread_id),
        key=_seq_of,
    )
    chain: list[dict[str, Any]] = []
    for expected_seq, r in enumerate(in_thread):
        if _seq_of(r) != expected_seq:
            break  # gap in the thread; stop the contiguous chain
        if str(r.get("status", "")).strip() != STATUS_APPROVED:
            break  # only publish the approved prefix
        chain.append(r)
    return chain


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
    poll_seconds: float = PUBLISH_STATUS_POLL_SECONDS,
    max_status_checks: int = PUBLISH_STATUS_MAX_CHECKS,
    reply_delay_seconds: float = THREAD_REPLY_DELAY_SECONDS,
) -> dict[str, int]:
    """Apply safety guards and publish at most `max_per_run` due UNITS.

    A unit is a single post or a whole thread (parent + replies). A thread
    publishes in one run (parent → replies in seq order) and consumes exactly
    one daily-cap / per-run slot. Returns counts: {"due", "posted", "failed"}
    where posted/failed are UNIT counts (a thread is 1).
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

    # Guard 4: per-run cap, counted in UNITS (a whole thread = 1 unit).
    if not due:
        logger.info("No due posts to publish")
        return base

    posted_at_iso = now_jst.strftime("%Y-%m-%dT%H:%M:%S%z")

    def mark_failed(row: dict[str, Any]) -> None:
        # Best-effort terminal mark; never raises (avoids masking the real error).
        try:
            _update_status(sheets, row, STATUS_FAILED)
        except Exception as upd_exc:
            logger.error("Could not mark queue_id=%s failed: %r", row.get("queue_id"), upd_exc)

    def wait_fn(creation_id: str) -> None:
        wait_until_finished(
            threads, creation_id, sleep_fn=sleep_fn,
            poll_seconds=poll_seconds, max_checks=max_status_checks,
        )

    def jitter() -> None:
        delay = max(0.0, float(jitter_fn()))
        if delay:
            logger.info("Jitter: sleeping %.0fs before publishing", delay)
            sleep_fn(delay)

    def publish_single(row: dict[str, Any]) -> bool:
        """Publish one standalone post; return True if posted."""
        queue_id = str(row.get("queue_id"))
        try:
            creation_id = threads.create_post(str(row.get("text", "")))
            wait_fn(creation_id)
            media_id = threads.publish_post(creation_id)
        except Exception as exc:
            logger.error("Publish failed for queue_id=%s: %r", queue_id, exc, exc_info=True)
            mark_failed(row)
            if log_fn is not None:
                log_fn("failed", 1, f"queue_id={queue_id}: 投稿失敗: {type(exc).__name__}: {exc}")
            return False
        try:
            _update_status(sheets, row, STATUS_POSTED, posted_post_id=media_id, posted_at=posted_at_iso)
            logger.info("Published queue_id=%s -> media_id=%s", queue_id, media_id)
            return True
        except Exception as exc:
            logger.error(
                "Published queue_id=%s (media_id=%s) but FAILED to record status: %r",
                queue_id, media_id, exc, exc_info=True,
            )
            mark_failed(row)
            if log_fn is not None:
                log_fn(
                    "failed", 1,
                    f"queue_id={queue_id}: 投稿成功・記録失敗（二重投稿防止でfailed扱い） "
                    f"media_id={media_id}: {type(exc).__name__}: {exc}",
                )
            return False

    def publish_thread_unit(chain: list[dict[str, Any]]) -> bool:
        """Publish a whole thread (parent + replies) as one unit; True if all posted."""
        tid = str(chain[0].get("thread_id", ""))

        def on_published(idx: int, media_id: str) -> None:
            r = chain[idx]
            _update_status(sheets, r, STATUS_POSTED, posted_post_id=media_id, posted_at=posted_at_iso)
            r["status"] = STATUS_POSTED
            r["posted_post_id"] = media_id

        try:
            media_ids = publish_thread(
                threads, [str(r.get("text", "")) for r in chain],
                sleep_fn=sleep_fn, wait_fn=wait_fn,
                delay_seconds=reply_delay_seconds, on_published=on_published,
            )
            logger.info("Published thread %s as 1 unit (%d posts)", tid, len(media_ids))
            return True
        except Exception as exc:
            logger.error("Thread %s publish interrupted: %r", tid, exc, exc_info=True)
            # Posted prefix stays posted (no double-post); mark the rest failed.
            for r in chain:
                if str(r.get("status", "")).strip() != STATUS_POSTED:
                    mark_failed(r)
            if log_fn is not None:
                log_fn("failed", 1, f"thread={tid}: ツリー投稿が中断: {type(exc).__name__}: {exc}")
            return False

    # Build due UNITS in scheduled order. A thread unit triggers only on its
    # parent (seq 0) being approved+due; its approved contiguous chain publishes
    # together. Reply rows that are "due" on their own are ignored here.
    units: list[tuple[str, Any]] = []
    seen_threads: set[str] = set()
    for row in due:
        thread_id = str(row.get("thread_id", "")).strip()
        if not thread_id:
            units.append(("single", row))
            continue
        if _seq_of(row) != 0 or thread_id in seen_threads:
            continue
        seen_threads.add(thread_id)
        chain = _approved_thread_chain(rows, thread_id)
        if chain:
            units.append(("thread", chain))

    posted = 0
    failed = 0
    for kind, unit in units:
        if posted >= max_per_run:
            break
        jitter()
        ok = publish_single(unit) if kind == "single" else publish_thread_unit(unit)
        if ok:
            posted += 1
        else:
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
