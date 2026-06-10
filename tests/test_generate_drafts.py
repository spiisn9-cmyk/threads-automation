"""Tests for F4 generate_drafts: writes DRAFT_COUNT drafts to post_queue."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from config.settings import (
    DRAFT_COUNT,
    POST_WINDOW_END_HOUR,
    POST_WINDOW_START_HOUR,
)
from src.core.queue import (
    JST,
    POST_QUEUE_HEADER,
    POST_QUEUE_SHEET,
    STATUS_DRAFT,
    parse_jst,
)
from src.jobs.generate_drafts import generate, parse_drafts

NOW = datetime(2026, 6, 10, 9, 0, 0, tzinfo=JST)


class FakeSheets:
    def __init__(self) -> None:
        self.sheets: dict[str, list[list]] = {
            "posts": [
                ["post_id", "posted_at", "text", "views", "likes"],
                ["p1", "2026-06-09", "伸びた投稿", "500", "30"],
                ["p2", "2026-06-08", "ふつうの投稿", "100", "5"],
            ],
            "metrics_daily": [
                ["date", "followers", "views", "follower_delta", "note"],
                ["2026-06-09", "27", "1000", "2", ""],
            ],
            POST_QUEUE_SHEET: [list(POST_QUEUE_HEADER)],
        }

    def read_rows(self, sheet, a1):
        return [list(r) for r in self.sheets.get(sheet, [])]

    def append_row(self, sheet, row):
        self.sheets.setdefault(sheet, []).append(list(row))

    def queue_data_rows(self):
        return self.sheets[POST_QUEUE_SHEET][1:]


class FakeClaude:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls: list[tuple[str, str]] = []

    def generate(self, system_prompt: str, user_content: str) -> str:
        self.calls.append((system_prompt, user_content))
        return self.payload


def _payload(n: int) -> str:
    return json.dumps(
        [{"theme": "実況・記録", "text": f"下書き本文 {i}"} for i in range(n)]
    )


def test_generate_writes_default_count_drafts():
    sheets = FakeSheets()
    claude = FakeClaude(_payload(DRAFT_COUNT))
    rows = generate(sheets, claude, "SYS", NOW)

    data = sheets.queue_data_rows()
    assert len(rows) == DRAFT_COUNT
    assert len(data) == DRAFT_COUNT

    status_idx = POST_QUEUE_HEADER.index("status")
    qid_idx = POST_QUEUE_HEADER.index("queue_id")
    sched_idx = POST_QUEUE_HEADER.index("scheduled_at")

    # all drafts, unique ids, one per day (tomorrow..+N), within the JST window
    assert all(r[status_idx] == STATUS_DRAFT for r in data)
    assert len({r[qid_idx] for r in data}) == DRAFT_COUNT

    dates = []
    for i, r in enumerate(data, start=1):
        dt = parse_jst(r[sched_idx])
        assert dt is not None
        expected_date = (NOW + timedelta(days=i)).strftime("%Y-%m-%d")
        assert dt.strftime("%Y-%m-%d") == expected_date  # one per consecutive day
        assert POST_WINDOW_START_HOUR <= dt.hour < POST_WINDOW_END_HOUR  # in window
        dates.append(dt.strftime("%Y-%m-%d"))
    assert dates[0] == "2026-06-11" and dates[-1] == "2026-06-17"


def test_generate_passes_post_content_without_numbers():
    sheets = FakeSheets()
    claude = FakeClaude(_payload(DRAFT_COUNT))
    generate(sheets, claude, "SYS", NOW)
    _, user_content = claude.calls[0]
    # post *content* is still referenced as a style hint...
    assert "伸びた投稿" in user_content
    # ...but the views/likes numbers are not surfaced.
    assert "views=500" not in user_content
    assert "likes=" not in user_content


def test_metrics_numbers_not_in_prompt():
    # FakeSheets.metrics_daily has followers=27, views=1000 — none of which
    # should reach the public-post generation prompt.
    sheets = FakeSheets()
    claude = FakeClaude(_payload(DRAFT_COUNT))
    generate(sheets, claude, "SYS", NOW)
    _, user_content = claude.calls[0]
    assert "27" not in user_content
    assert "1000" not in user_content
    assert "followers" not in user_content
    assert "最新のアカウント数値" not in user_content


def test_parse_drafts_strips_code_fence():
    raw = "```json\n" + _payload(2) + "\n```"
    drafts = parse_drafts(raw, 2)
    assert len(drafts) == 2
    assert drafts[0][0] == "実況・記録"


def test_parse_drafts_rejects_non_json():
    try:
        parse_drafts("not json at all", 7)
    except RuntimeError:
        return
    raise AssertionError("expected RuntimeError for non-JSON response")
