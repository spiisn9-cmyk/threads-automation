"""Generate uses technique tags + rating + feedback from posts AND post_queue."""
from __future__ import annotations

import json
from datetime import datetime

from config.settings import DAILY_DRAFT_COUNT
from src.core.queue import JST, POST_QUEUE_HEADER, POST_QUEUE_SHEET, STATUS_POSTED
from src.jobs.generate_drafts import generate

NOW = datetime(2026, 6, 10, 9, 0, 0, tzinfo=JST)
POSTS_HEADER = ["post_id", "posted_at", "text", "views", "likes", "rating", "feedback", "tags"]


def _qrow(**kw):
    base = {c: "" for c in POST_QUEUE_HEADER}
    base.update(kw)
    return [base[c] for c in POST_QUEUE_HEADER]


class FakeSheets:
    def __init__(self) -> None:
        self.sheets = {
            "posts": [
                POSTS_HEADER,
                # good post with strong technique tags
                ["p1", "2026-06-09", "問いかけで始めた投稿", "500", "40", "good", "", "問いかけ | 具体・数字"],
            ],
            POST_QUEUE_SHEET: [
                list(POST_QUEUE_HEADER),
                # a bad-rated candidate with a one-line note + tag
                _qrow(queue_id="qb", text="硬い説明調の下書き", status="draft",
                      rating="bad", feedback="説明調すぎる", tags="リスト・まとめ"),
            ],
        }

    def read_rows(self, sheet, a1):
        return [list(r) for r in self.sheets.get(sheet, [])]

    def append_row(self, sheet, row):
        self.sheets.setdefault(sheet, []).append(list(row))

    def update_row(self, sheet, a1, row):
        idx = int(a1[1:])
        self.sheets[sheet][idx - 1] = list(row)


class FakeClaude:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def generate(self, system_prompt, user_content):
        self.calls.append((system_prompt, user_content))
        return self.payload


def _payload(n):
    return json.dumps([{"theme": "学び・気づき", "text": f"本文{i}"} for i in range(n)])


def test_technique_feedback_reaches_prompt():
    sheets = FakeSheets()
    claude = FakeClaude(_payload(DAILY_DRAFT_COUNT))
    generate(sheets, claude, "SYS", NOW)
    _, uc = claude.calls[0]

    assert "技法フィードバック" in uc
    # good technique tags surfaced as "lean toward"
    assert "問いかけ" in uc and "具体・数字" in uc
    assert "寄せる" in uc
    # bad-rated candidate's tag + one-line note surfaced as "reinforce/avoid"
    assert "リスト・まとめ" in uc
    assert "説明調すぎる" in uc
    assert ("補強" in uc) or ("回避" in uc)
