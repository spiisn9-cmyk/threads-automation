"""Tests for thread (連投) support: generation expansion + sequential publishing."""
from __future__ import annotations

import json
from datetime import datetime

from src.core.queue import (
    JST,
    POST_QUEUE_HEADER,
    POST_QUEUE_SHEET,
    STATUS_APPROVED,
    STATUS_POSTED,
)
from src.jobs.generate_drafts import build_queue_rows, parse_drafts
from src.jobs.publish_queue import publish_due, publish_thread, reply_target

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
    assert drafts[1][2] == ()  # no thread => empty replies


def test_build_queue_rows_expands_thread():
    drafts = [("学び・気づき", "親本文", ("返信1", "返信2"))]
    rows = build_queue_rows(drafts, NOW, 3)
    assert len(rows) == 3  # parent + 2 replies

    parent = rows[0]
    tid = parent[HI["thread_id"]]
    assert tid and parent[HI["seq"]] == 0
    assert rows[1][HI["thread_id"]] == tid and rows[1][HI["seq"]] == 1
    assert rows[2][HI["thread_id"]] == tid and rows[2][HI["seq"]] == 2
    assert rows[1][HI["text"]] == "返信1" and rows[2][HI["text"]] == "返信2"


def test_build_queue_rows_single_has_no_thread():
    rows = build_queue_rows([("学び", "本文", ())], NOW, 3)
    assert rows[0][HI["thread_id"]] == "" and rows[0][HI["seq"]] == 0


# --- publish_thread: sequential chaining ---

class FakeThreads:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.reply_targets: list[str | None] = []
        self.published: list[str] = []

    def create_post(self, text, reply_to_id=None):
        self.created.append(text)
        self.reply_targets.append(reply_to_id)
        return f"creation-{len(self.created)}"

    def get_container_status(self, creation_id):
        return {"status": "FINISHED", "error_message": ""}

    def publish_post(self, creation_id):
        self.published.append(creation_id)
        return f"media-{len(self.published)}"


def test_publish_thread_chains_each_reply_to_previous():
    t = FakeThreads()
    media = publish_thread(
        t, ["親", "返信1", "返信2"],
        sleep_fn=lambda s: None, wait_fn=lambda c: None, delay_seconds=0,
    )
    assert media == ["media-1", "media-2", "media-3"]
    assert t.created == ["親", "返信1", "返信2"]
    # parent has no reply target; each reply points at the previous media id
    assert t.reply_targets == [None, "media-1", "media-2"]


# --- reply_target resolution ---

def _r(qid, seq, status, posted_id=""):
    return {
        "queue_id": qid, "thread_id": "T", "seq": seq,
        "status": status, "posted_post_id": posted_id, "text": qid,
    }


def test_reply_target_defers_until_predecessor_posted():
    rows = [_r("p", 0, STATUS_APPROVED), _r("r1", 1, STATUS_APPROVED)]
    rt, deferred = reply_target(rows, rows[1])
    assert rt is None and deferred is True


def test_reply_target_resolves_to_predecessor_media():
    rows = [_r("p", 0, STATUS_POSTED, "media-9"), _r("r1", 1, STATUS_APPROVED)]
    rt, deferred = reply_target(rows, rows[1])
    assert rt == "media-9" and deferred is False


def test_reply_target_parent_is_not_a_reply():
    rows = [_r("p", 0, STATUS_APPROVED)]
    assert reply_target(rows, rows[0]) == (None, False)


# --- publish_due integration (FakeSheets) ---

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
    return publish_due(
        sheets, threads, NOON, sleep_fn=NOOP, jitter_fn=lambda: 0, **kw
    )


def test_default_posts_parent_then_defers_reply_same_run():
    # parent + reply both approved & due; per-run cap 1 -> parent posts, reply waits
    sheets = FakeSheets(
        [
            _qrow(queue_id="p", thread_id="T", seq=0, status=STATUS_APPROVED,
                  text="親", scheduled_at="2026-06-10 09:00"),
            _qrow(queue_id="r1", thread_id="T", seq=1, status=STATUS_APPROVED,
                  text="返信1", scheduled_at="2026-06-10 09:01"),
        ]
    )
    threads = FakeThreads()
    result = _publish(sheets, threads)
    assert result["posted"] == 1  # only the parent this run
    by = {d["queue_id"]: d for d in sheets.dicts()}
    assert by["p"]["status"] == STATUS_POSTED
    assert by["r1"]["status"] == STATUS_APPROVED  # reply deferred


def test_reply_posts_with_reply_to_once_parent_posted():
    # parent already posted in a previous run; this run posts the reply chained
    sheets = FakeSheets(
        [
            _qrow(queue_id="p", thread_id="T", seq=0, status=STATUS_POSTED,
                  posted_post_id="media-parent", posted_at="2026-06-10 08:00",
                  text="親", scheduled_at="2026-06-10 08:00"),
            _qrow(queue_id="r1", thread_id="T", seq=1, status=STATUS_APPROVED,
                  text="返信1", scheduled_at="2026-06-10 09:01"),
        ]
    )
    threads = FakeThreads()
    result = _publish(sheets, threads, min_hours=0, max_per_day=99)
    assert result["posted"] == 1
    assert threads.reply_targets == ["media-parent"]  # replied to the parent
    by = {d["queue_id"]: d for d in sheets.dicts()}
    assert by["r1"]["status"] == STATUS_POSTED


def test_inline_mode_publishes_whole_chain():
    sheets = FakeSheets(
        [
            _qrow(queue_id="p", thread_id="T", seq=0, status=STATUS_APPROVED,
                  text="親", scheduled_at="2026-06-10 09:00"),
            _qrow(queue_id="r1", thread_id="T", seq=1, status=STATUS_APPROVED,
                  text="返信1", scheduled_at="2026-06-10 09:01"),
            _qrow(queue_id="r2", thread_id="T", seq=2, status=STATUS_APPROVED,
                  text="返信2", scheduled_at="2026-06-10 09:02"),
        ]
    )
    threads = FakeThreads()
    result = _publish(sheets, threads, thread_inline=True, reply_delay_seconds=0)
    assert result["posted"] == 3  # whole chain in one run
    assert threads.reply_targets == [None, "media-1", "media-2"]  # chained
    assert all(d["status"] == STATUS_POSTED for d in sheets.dicts())
