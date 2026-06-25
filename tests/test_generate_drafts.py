"""Tests for F4 generate_drafts: writes DAILY_DRAFT_COUNT candidate drafts to post_queue."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from config.settings import (
    DAILY_DRAFT_COUNT,
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


def test_generate_writes_daily_count_candidates_for_tomorrow():
    sheets = FakeSheets()
    claude = FakeClaude(_payload(DAILY_DRAFT_COUNT))
    rows = generate(sheets, claude, "SYS", NOW)

    data = sheets.queue_data_rows()
    assert len(rows) == DAILY_DRAFT_COUNT
    assert len(data) == DAILY_DRAFT_COUNT

    status_idx = POST_QUEUE_HEADER.index("status")
    qid_idx = POST_QUEUE_HEADER.index("queue_id")
    sched_idx = POST_QUEUE_HEADER.index("scheduled_at")

    # all drafts, unique ids, all candidates for TOMORROW within the JST window
    assert all(r[status_idx] == STATUS_DRAFT for r in data)
    assert len({r[qid_idx] for r in data}) == DAILY_DRAFT_COUNT
    for r in data:
        dt = parse_jst(r[sched_idx])
        assert dt is not None
        assert dt.strftime("%Y-%m-%d") == "2026-06-11"  # next day
        assert POST_WINDOW_START_HOUR <= dt.hour < POST_WINDOW_END_HOUR


def test_generate_passes_post_content_without_numbers():
    sheets = FakeSheets()
    claude = FakeClaude(_payload(DAILY_DRAFT_COUNT))
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
    claude = FakeClaude(_payload(DAILY_DRAFT_COUNT))
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


def test_fb_sections_have_meta_guard():
    """FB/rating/learning sections must include a "do not use as topic" guard."""
    from src.core.queue import POST_QUEUE_HEADER, POST_QUEUE_SHEET
    from src.core.learnings import LEARNINGS_HEADER, LEARNINGS_SHEET

    sheets = FakeSheets()
    # Inject a rated post so the rating FB section appears.
    sheets.sheets["posts"] = [
        ["post_id", "posted_at", "text", "views", "likes", "rating", "feedback"],
        ["p1", "2026-06-09", "伸びた投稿", "500", "30", "good", ""],
    ]
    # Inject a learning so the learning section appears.
    sheets.sheets[LEARNINGS_SHEET] = [
        list(LEARNINGS_HEADER),
        ["2026-06-09", "問いかけ型が効く", "good評価", "auto"],
    ]
    claude = FakeClaude(_payload(DAILY_DRAFT_COUNT))
    generate(sheets, claude, "SYS", NOW)
    _, uc = claude.calls[0]
    # The guard phrase must appear in both sections.
    assert "題材として絶対に使わない" in uc
    assert "書き方・方向性の調整" in uc


def test_recent_posts_passed_for_diversity():
    """Direct recent posts must reach the prompt for anti-repeat steering."""
    sheets = FakeSheets()
    claude = FakeClaude(_payload(DAILY_DRAFT_COUNT))
    generate(sheets, claude, "SYS", NOW)
    _, uc = claude.calls[0]
    # Both recent posts appear in the diversity section.
    assert "伸びた投稿" in uc
    assert "ふつうの投稿" in uc
    # The diversity instruction is present.
    assert "書き出し・テンプレCTA・切り口・構成が" in uc


def test_references_buzz_analysis_instruction_in_prompt():
    """The prompt must ask Claude to analyse winning patterns from references."""
    sheets = FakeSheets()
    # Seed a reference so the section appears.
    from src.core.references import REFERENCES_HEADER, REFERENCES_SHEET
    sheets.sheets[REFERENCES_SHEET] = [
        list(REFERENCES_HEADER),
        ["2026-06-01", "@x", "", "勝ちパターン参考投稿", "", "active", "", "FALSE"],
    ]
    claude = FakeClaude(_payload(DAILY_DRAFT_COUNT))
    generate(sheets, claude, "SYS", NOW)
    _, uc = claude.calls[0]
    assert "勝ちパターン" in uc
    assert "新しい角度" in uc
