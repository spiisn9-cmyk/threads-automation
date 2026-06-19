"""Tests for thread (連投): generation expansion + unit-based publishing.

Publish model: a whole thread (parent + replies) is ONE unit — it publishes in
a single run (parent → replies in seq order, replies reply_to the PARENT),
consumes exactly one daily-cap / per-run slot, and fails as a unit.
"""
from __future__ import annotations

import json
from datetime import datetime

from src.core.queue import (
    JST,
    POST_QUEUE_HEADER,
    POST_QUEUE_SHEET,
    STATUS_APPROVED,
    STATUS_FAILED,
    STATUS_POSTED,
)
from src.jobs.generate_drafts import build_queue_rows, parse_drafts
from src.jobs.publish_queue import publish_due, publish_thread

NOW = datetime(2026, 6, 10, 9, 0, 0, tzinfo=JST)
NOON = datetime(2026, 6, 10, 12, 0, 0, tzinfo=JST)
HI = {c: i for i, c in enumerate(POST_QUEUE_HEADER)}


# --- generation: parse + expand ---

def test_parse_drafts_captures_thread_replies():
    raw = json.dumps(
        [
            {"theme": "学び・気づき", "text": "親", "thread": ["返信1", "返信2"]},
            {"theme": "本音・共感", "text": "単発"},
        ]
    )
    drafts = parse_drafts(raw, 2)
    assert drafts[0][1] == "親" and drafts[0][2] == ("返信1", "返信2")
    assert drafts[1][2] == ()


def test_build_queue_rows_expands_thread():
    rows = build_queue_rows([("学び・気づき", "親本文", ("返信1", "返信2"))], NOW, 3)
    assert len(rows) == 3
    parent = rows[0]
    tid = parent[HI["thread_id"]]
    assert tid and parent[HI["seq"]] == 0
    assert rows[1][HI["thread_id"]] == tid and rows[1][HI["seq"]] == 1
    assert rows[2][HI["thread_id"]] == tid and rows[2][HI["seq"]] == 2
    assert rows[1][HI["text"]] == "返信1" and rows[2][HI["text"]] == "返信2"


def test_build_queue_rows_single_has_no_thread():
    rows = build_queue_rows([("学び", "本文", ())], NOW, 3)
    assert rows[0][HI["thread_id"]] == "" and rows[0][HI["seq"]] == 0


# --- fakes ---

class FakeThreads:
    def __init__(self, fail_publish_at: int | None = None) -> None:
        self.created: list[str] = []
        self.reply_targets: list[str | None] = []
        self.published: list[str] = []
        self.fail_publish_at = fail_publish_at
        self._pub = 0

    def create_post(self, text, reply_to_id=None):
        self.created.append(text)
        self.reply_targets.append(reply_to_id)
        return f"creation-{len(self.created)}"

    def get_container_status(self, creation_id):
        return {"status": "FINISHED", "error_message": ""}

    def publish_post(self, creation_id):
        self._pub += 1
        if self.fail_publish_at and self._pub == self.fail_publish_at:
            raise RuntimeError("publish boom")
        self.published.append(creation_id)
        return f"media-{len(self.published)}"


def _qrow(**kw):
    base = {c: "" for c in POST_QUEUE_HEADER}
    base.update(kw)
    return [base[c] for c in POST_QUEUE_HEADER]


class FakeSheets:
    def __init__(self, rows):
        self.sheets = {
            POST_QUEUE_SHEET: [list(POST_QUEUE_HEADER)] + rows,
            "logs": [["datetime", "job", "status", "count", "message"]],
        }

    def read_rows(self, sheet, a1):
        return [list(r) for r in self.sheets.get(sheet, [])]

    def upsert_row(self, sheet, key_col, key_val, row_dict):
        grid = self.sheets[sheet]
        header = grid[0]
        ki = header.index(key_col)
        ordered = [row_dict.get(c, "") for c in header]
        for i, row in enumerate(grid[1:], start=1):
            if len(row) > ki and row[ki] == key_val:
                grid[i] = ordered
                return
        grid.append(ordered)

    def append_row(self, sheet, row):
        self.sheets.setdefault(sheet, []).append(list(row))

    def dicts(self):
        g = self.sheets[POST_QUEUE_SHEET]
        return [dict(zip(g[0], r)) for r in g[1:]]


NOOP = lambda *_: None  # noqa: E731


def _publish(sheets, threads, **kw):
    return publish_due(sheets, threads, NOON, sleep_fn=NOOP, jitter_fn=lambda: 0, **kw)


def _thread_rows():
    return [
        _qrow(queue_id="p", thread_id="T", seq=0, status=STATUS_APPROVED,
              text="親", scheduled_at="2026-06-10 09:00"),
        _qrow(queue_id="r1", thread_id="T", seq=1, status=STATUS_APPROVED,
              text="返信1", scheduled_at="2026-06-10 09:01"),
        _qrow(queue_id="r2", thread_id="T", seq=2, status=STATUS_APPROVED,
              text="返信2", scheduled_at="2026-06-10 09:02"),
    ]


# --- publish_thread: replies point at the parent ---

def test_publish_thread_replies_to_parent():
    t = FakeThreads()
    media = publish_thread(
        t, ["親", "返信1", "返信2"],
        sleep_fn=NOOP, wait_fn=lambda c: None, delay_seconds=0,
    )
    assert media == ["media-1", "media-2", "media-3"]
    assert t.created == ["親", "返信1", "返信2"]
    # parent has no target; every reply replies to the PARENT (media-1)
    assert t.reply_targets == [None, "media-1", "media-1"]


# --- publish_due: thread as one unit ---

def test_thread_publishes_whole_chain_in_one_run_as_one_unit():
    sheets = FakeSheets(_thread_rows())
    threads = FakeThreads()
    result = _publish(sheets, threads)
    assert result["posted"] == 1  # the thread counts as ONE unit
    assert threads.created == ["親", "返信1", "返信2"]  # parent then replies, seq order
    assert threads.reply_targets == [None, "media-1", "media-1"]
    assert all(d["status"] == STATUS_POSTED for d in sheets.dicts())


def test_thread_consumes_only_one_daily_slot():
    # a thread already fully posted today (3 rows) = 1 unit; with cap 1 a new
    # standalone is blocked (replies must NOT inflate the daily count).
    rows = [
        _qrow(queue_id="p", thread_id="T", seq=0, status=STATUS_POSTED,
              posted_post_id="m0", posted_at="2026-06-10 08:00", text="親"),
        _qrow(queue_id="r1", thread_id="T", seq=1, status=STATUS_POSTED,
              posted_post_id="m1", posted_at="2026-06-10 08:00", text="返信1"),
        _qrow(queue_id="r2", thread_id="T", seq=2, status=STATUS_POSTED,
              posted_post_id="m2", posted_at="2026-06-10 08:00", text="返信2"),
        _qrow(queue_id="s", status=STATUS_APPROVED, text="単発",
              scheduled_at="2026-06-10 09:00"),
    ]
    sheets = FakeSheets(rows)
    threads = FakeThreads()
    logs: list[tuple] = []
    result = publish_due(
        sheets, threads, NOON, sleep_fn=NOOP, jitter_fn=lambda: 0,
        max_per_day=1, log_fn=lambda *a: logs.append(a),
    )
    assert result["posted"] == 0  # daily cap (1 unit) already consumed by the thread
    assert threads.created == []
    assert any("日次上限" in a[2] for a in logs)


def test_per_run_cap_is_one_unit():
    # a standalone (earlier) and a thread both due; max_per_run=1 -> only 1 unit
    rows = [
        _qrow(queue_id="s", status=STATUS_APPROVED, text="単発",
              scheduled_at="2026-06-10 09:00"),
    ] + [
        _qrow(queue_id="p", thread_id="T", seq=0, status=STATUS_APPROVED,
              text="親", scheduled_at="2026-06-10 09:30"),
        _qrow(queue_id="r1", thread_id="T", seq=1, status=STATUS_APPROVED,
              text="返信1", scheduled_at="2026-06-10 09:31"),
    ]
    sheets = FakeSheets(rows)
    threads = FakeThreads()
    result = _publish(sheets, threads, max_per_day=99)
    assert result["posted"] == 1
    assert threads.created == ["単発"]  # earliest unit only
    by = {d["queue_id"]: d for d in sheets.dicts()}
    assert by["s"]["status"] == STATUS_POSTED
    assert by["p"]["status"] == STATUS_APPROVED  # thread carried over


def test_partial_thread_failure_marks_remainder_failed():
    sheets = FakeSheets(_thread_rows())
    threads = FakeThreads(fail_publish_at=2)  # parent ok, first reply fails
    logs: list[tuple] = []
    result = publish_due(
        sheets, threads, NOON, sleep_fn=NOOP, jitter_fn=lambda: 0,
        log_fn=lambda *a: logs.append(a),
    )
    assert result == {"due": 3, "posted": 0, "failed": 1}  # thread fails as a unit
    by = {d["queue_id"]: d for d in sheets.dicts()}
    assert by["p"]["status"] == STATUS_POSTED  # parent already went out
    assert by["r1"]["status"] == STATUS_FAILED
    assert by["r2"]["status"] == STATUS_FAILED
    assert any("ツリー投稿が中断" in a[2] for a in logs)
