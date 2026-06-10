"""Tests for F5 publish_queue safety guards.

Verifies: at most 1 post per run (no bursts), time-window guard, daily cap,
minimum interval, drafts never published, posted rows never reprocessed.
now() is injected; sleep/jitter are no-ops so tests don't actually wait.
"""
from __future__ import annotations

from datetime import datetime

from src.core.queue import (
    JST,
    POST_QUEUE_HEADER,
    POST_QUEUE_SHEET,
    STATUS_APPROVED,
    STATUS_DRAFT,
    STATUS_POSTED,
)
from src.jobs.publish_queue import publish_due

# 2026-06-10 12:00 JST — inside the 8..22 window.
NOON = datetime(2026, 6, 10, 12, 0, 0, tzinfo=JST)
PAST = "2026-06-10T09:00:00+0900"
EARLIER = "2026-06-10T08:00:00+0900"
FUTURE = "2026-06-11T12:00:00+0900"

NOOP_SLEEP = lambda *_: None  # noqa: E731
NO_JITTER = lambda: 0.0  # noqa: E731


def run_publish(sheets, threads, now, log=None, **kw):
    """Call publish_due with no real sleeping/jitter."""
    return publish_due(
        sheets, threads, now, log_fn=log, sleep_fn=NOOP_SLEEP, jitter_fn=NO_JITTER, **kw
    )


class FakeSheets:
    def __init__(self, queue_rows: list[dict]) -> None:
        self.sheets: dict[str, list[list]] = {
            POST_QUEUE_SHEET: [list(POST_QUEUE_HEADER)]
            + [[r.get(c, "") for c in POST_QUEUE_HEADER] for r in queue_rows],
            "logs": [["datetime", "job", "status", "count", "message"]],
        }

    def read_rows(self, sheet, a1):
        return [list(r) for r in self.sheets.get(sheet, [])]

    def upsert_row(self, sheet, key_col, key_val, row_dict):
        grid = self.sheets[sheet]
        header = grid[0]
        key_idx = header.index(key_col)
        ordered = [row_dict.get(c, "") for c in header]
        for i, row in enumerate(grid[1:], start=1):
            if len(row) > key_idx and row[key_idx] == key_val:
                grid[i] = ordered
                return
        grid.append(ordered)

    def append_row(self, sheet, row):
        self.sheets.setdefault(sheet, []).append(list(row))

    def queue_dicts(self):
        grid = self.sheets[POST_QUEUE_SHEET]
        header = grid[0]
        return [dict(zip(header, row)) for row in grid[1:]]


class FakeThreads:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.published: list[str] = []

    def create_post(self, text: str) -> str:
        self.created.append(text)
        return f"creation-{len(self.created)}"

    def publish_post(self, creation_id: str) -> str:
        self.published.append(creation_id)
        return f"media-{len(self.published)}"


def _row(queue_id, status, scheduled_at, text="hello", posted_at=""):
    return {
        "queue_id": queue_id,
        "scheduled_at": scheduled_at,
        "text": text,
        "theme": "実況・記録",
        "status": status,
        "posted_post_id": "",
        "posted_at": posted_at,
    }


def test_at_most_one_post_per_run_even_with_many_due():
    sheets = FakeSheets(
        [
            _row("late", STATUS_APPROVED, "2026-06-10T11:00:00+0900", text="B"),
            _row("early", STATUS_APPROVED, "2026-06-10T09:00:00+0900", text="A"),
            _row("early2", STATUS_APPROVED, EARLIER, text="A0"),
        ]
    )
    threads = FakeThreads()
    result = run_publish(sheets, threads, NOON)

    assert result["posted"] == 1, "never burst — only one per run"
    assert threads.created == ["A0"], "earliest scheduled_at goes first"
    by_id = {r["queue_id"]: r for r in sheets.queue_dicts()}
    assert by_id["early2"]["status"] == STATUS_POSTED
    assert by_id["early2"]["posted_post_id"] == "media-1"
    assert by_id["early2"]["posted_at"].startswith("2026-06-10T12:00:00")
    # the rest carry over, untouched
    assert by_id["early"]["status"] == STATUS_APPROVED
    assert by_id["late"]["status"] == STATUS_APPROVED


def test_drafts_never_published():
    sheets = FakeSheets([_row("q1", STATUS_DRAFT, PAST)])
    threads = FakeThreads()
    result = run_publish(sheets, threads, NOON)
    assert result["posted"] == 0
    assert threads.created == []


def test_posted_rows_not_reprocessed():
    sheets = FakeSheets(
        [{**_row("q1", STATUS_POSTED, PAST, posted_at=PAST), "posted_post_id": "media-x"}]
    )
    threads = FakeThreads()
    result = run_publish(sheets, threads, NOON)
    assert result == {"due": 0, "posted": 0, "failed": 0}
    assert threads.created == []
    assert sheets.queue_dicts()[0]["posted_post_id"] == "media-x"


def test_out_of_window_before_start_posts_nothing():
    early_morning = datetime(2026, 6, 10, 6, 0, 0, tzinfo=JST)  # before 8
    sheets = FakeSheets([_row("q1", STATUS_APPROVED, PAST)])
    threads = FakeThreads()
    logs: list[tuple] = []
    result = run_publish(sheets, threads, early_morning, log=lambda *a: logs.append(a))
    assert result["posted"] == 0
    assert threads.created == []
    assert any("時間帯外" in a[2] for a in logs)


def test_out_of_window_at_end_hour_posts_nothing():
    night = datetime(2026, 6, 10, 22, 0, 0, tzinfo=JST)  # == END (exclusive)
    sheets = FakeSheets([_row("q1", STATUS_APPROVED, PAST)])
    result = run_publish(sheets, FakeThreads(), night)
    assert result["posted"] == 0


def test_daily_cap_reached_posts_nothing():
    # one post already today (7:00, >4h before noon so interval is NOT the blocker)
    sheets = FakeSheets(
        [
            _row("done", STATUS_POSTED, EARLIER, posted_at="2026-06-10T07:00:00+0900"),
            _row("q1", STATUS_APPROVED, PAST),
        ]
    )
    threads = FakeThreads()
    logs: list[tuple] = []
    result = run_publish(
        sheets, threads, NOON, log=lambda *a: logs.append(a), max_per_day=1
    )
    assert result["posted"] == 0
    assert threads.created == []
    assert any("日次上限" in a[2] for a in logs)


def test_min_interval_blocks_recent_post():
    # last post 2h ago; min interval 4h. Raise daily cap so interval is the blocker.
    sheets = FakeSheets(
        [
            _row("done", STATUS_POSTED, PAST, posted_at="2026-06-10T10:00:00+0900"),
            _row("q1", STATUS_APPROVED, PAST),
        ]
    )
    threads = FakeThreads()
    logs: list[tuple] = []
    result = run_publish(
        sheets, threads, NOON, log=lambda *a: logs.append(a), max_per_day=99
    )
    assert result["posted"] == 0
    assert threads.created == []
    assert any("最小間隔" in a[2] for a in logs)


def test_min_interval_ok_allows_post():
    # last post 5h ago (>4h); daily cap raised so only interval matters.
    sheets = FakeSheets(
        [
            _row("done", STATUS_POSTED, EARLIER, posted_at="2026-06-10T07:00:00+0900"),
            _row("q1", STATUS_APPROVED, PAST),
        ]
    )
    threads = FakeThreads()
    result = run_publish(sheets, threads, NOON, max_per_day=99)
    assert result["posted"] == 1
    assert threads.created == ["hello"]


def test_jitter_sleep_runs_before_publish():
    sheets = FakeSheets([_row("q1", STATUS_APPROVED, PAST)])
    slept: list[float] = []
    publish_due(
        sheets,
        FakeThreads(),
        NOON,
        sleep_fn=lambda s: slept.append(s),
        jitter_fn=lambda: 123.0,
    )
    assert slept == [123.0]


def test_failed_post_marked_failed_and_logged():
    class FailingThreads(FakeThreads):
        def create_post(self, text: str) -> str:
            raise RuntimeError("boom")

    sheets = FakeSheets([_row("q1", STATUS_APPROVED, PAST)])
    logs: list[tuple] = []
    result = run_publish(sheets, FailingThreads(), NOON, log=lambda *a: logs.append(a))
    assert result == {"due": 1, "posted": 0, "failed": 1}
    assert sheets.queue_dicts()[0]["status"] == "failed"
    assert any(a[0] == "failed" for a in logs)
