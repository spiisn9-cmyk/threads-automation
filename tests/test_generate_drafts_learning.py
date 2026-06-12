"""Tests that generate_drafts feeds the learning loop materials to the model."""
from __future__ import annotations

import json
from datetime import datetime

from config.settings import DAILY_DRAFT_COUNT
from src.core.learnings import LEARNINGS_HEADER, LEARNINGS_SHEET
from src.core.notes import NOTES_HEADER, NOTES_SHEET, NOTE_STATUS_NEW, NOTE_STATUS_USED
from src.core.queue import JST, POST_QUEUE_HEADER, POST_QUEUE_SHEET
from src.core.references import REFERENCES_HEADER, REFERENCES_SHEET
from src.jobs.generate_drafts import generate

NOW = datetime(2026, 6, 10, 9, 0, 0, tzinfo=JST)

POSTS_HEADER = ["post_id", "posted_at", "text", "views", "likes", "rating", "feedback"]


class FakeSheets:
    def __init__(self) -> None:
        self.sheets: dict[str, list[list]] = {
            "posts": [
                POSTS_HEADER,
                ["p1", "2026-06-10", "良い投稿だよね", "300", "20", "good", ""],
                ["p2", "2026-06-09", "硬くて低反応だった話", "10", "0", "bad", ""],
            ],
            LEARNINGS_SHEET: [
                list(LEARNINGS_HEADER),
                ["2026-06-10", "問いかけ型が効くかも", "good評価2件", "auto"],
            ],
            REFERENCES_SHEET: [
                list(REFERENCES_HEADER),
                ["2026-06-01", "@x", "9000", "結局どこから始める？", "問いかけ型", "active"],
            ],
            NOTES_SHEET: [
                list(NOTES_HEADER),
                ["2026-06-10", "今日はAPI実装でハマった", "実況・記録", NOTE_STATUS_NEW],
            ],
            POST_QUEUE_SHEET: [list(POST_QUEUE_HEADER)],
        }

    def read_rows(self, sheet, a1):
        return [list(r) for r in self.sheets.get(sheet, [])]

    def append_row(self, sheet, row):
        self.sheets.setdefault(sheet, []).append(list(row))

    def update_row(self, sheet, a1, row):
        idx = int(a1[1:])
        self.sheets[sheet][idx - 1] = list(row)

    def notes_rows(self):
        return self.sheets[NOTES_SHEET][1:]


class FakeClaude:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls: list[tuple[str, str]] = []

    def generate(self, system_prompt: str, user_content: str) -> str:
        self.calls.append((system_prompt, user_content))
        return self.payload


def _payload(n: int) -> str:
    return json.dumps([{"theme": "実況・記録", "text": f"本文{i}"} for i in range(n)])


def test_all_learning_materials_reach_the_prompt():
    sheets = FakeSheets()
    claude = FakeClaude(_payload(DAILY_DRAFT_COUNT))
    generate(sheets, claude, "SYS", NOW)
    _, uc = claude.calls[0]

    # learnings
    assert "問いかけ型が効くかも" in uc
    # ratings: good steered toward, bad avoided
    assert "良い投稿だよね" in uc
    assert "硬くて低反応だった話" in uc
    assert "good評価" in uc and "bad評価" in uc
    # references (structure) + no-copy
    assert "結局どこから始める？" in uc
    assert "丸写し" in uc
    # notes (content material)
    assert "今日はAPI実装でハマった" in uc
    # explicit improvement instruction
    assert "避け" in uc


def test_bad_rated_posts_are_listed_as_avoid():
    sheets = FakeSheets()
    claude = FakeClaude(_payload(DAILY_DRAFT_COUNT))
    generate(sheets, claude, "SYS", NOW)
    _, uc = claude.calls[0]
    # the bad-rated post is under an "avoid" marker, not the "good" one
    avoid_section = uc.split("bad評価")[1]
    assert "硬くて低反応だった話" in avoid_section


def test_note_is_marked_used_in_learning_run():
    sheets = FakeSheets()
    generate(sheets, FakeClaude(_payload(DAILY_DRAFT_COUNT)), "SYS", NOW)
    statuses = [r[NOTES_HEADER.index("status")] for r in sheets.notes_rows()]
    assert statuses == [NOTE_STATUS_USED]
