"""Tests for the 小言メモ→投稿素材 feature in generate_drafts."""
from __future__ import annotations

import json
from datetime import datetime

from config.settings import DAILY_DRAFT_COUNT
from src.core.notes import NOTES_HEADER, NOTES_SHEET, NOTE_STATUS_NEW, NOTE_STATUS_USED
from src.core.queue import JST, POST_QUEUE_HEADER, POST_QUEUE_SHEET
from src.jobs.generate_drafts import generate

NOW = datetime(2026, 6, 10, 9, 0, 0, tzinfo=JST)


class FakeSheets:
    def __init__(self, notes: list[list]) -> None:
        self.sheets: dict[str, list[list]] = {
            "posts": [["post_id", "posted_at", "text", "views", "likes"]],
            "metrics_daily": [
                ["date", "followers", "views", "follower_delta", "note"],
                ["2026-06-09", "27", "1000", "2", ""],
            ],
            NOTES_SHEET: [list(NOTES_HEADER)] + notes,
            POST_QUEUE_SHEET: [list(POST_QUEUE_HEADER)],
        }

    def read_rows(self, sheet, a1):
        return [list(r) for r in self.sheets.get(sheet, [])]

    def append_row(self, sheet, row):
        self.sheets.setdefault(sheet, []).append(list(row))

    def update_row(self, sheet, a1, row):
        # a1 like "A{n}" -> 1-based row index
        idx = int(a1[1:])
        self.sheets[sheet][idx - 1] = list(row)

    def notes_rows(self):
        return self.sheets[NOTES_SHEET][1:]

    def queue_rows(self):
        return self.sheets[POST_QUEUE_SHEET][1:]


class FakeClaude:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls: list[tuple[str, str]] = []

    def generate(self, system_prompt: str, user_content: str) -> str:
        self.calls.append((system_prompt, user_content))
        return self.payload


def _payload(n: int) -> str:
    return json.dumps([{"theme": "本音・共感", "text": f"本文{i}"} for i in range(n)])


def _note(created_at, text, theme="本音・共感", status=NOTE_STATUS_NEW):
    return [created_at, text, theme, status]


def test_new_notes_are_used_and_marked_used():
    sheets = FakeSheets(
        [
            _note("2026-06-09T20:00", "今日は3時間溶けた"),
            _note("2026-06-09T21:00", "やっと1件売れた、うれしい"),
        ]
    )
    claude = FakeClaude(_payload(DAILY_DRAFT_COUNT))
    rows = generate(sheets, claude, "SYS", NOW)

    assert len(rows) == DAILY_DRAFT_COUNT
    # the note text is surfaced to the model as priority material
    _, user_content = claude.calls[0]
    assert "今日は3時間溶けた" in user_content
    assert "やっと1件売れた" in user_content

    statuses = [r[NOTES_HEADER.index("status")] for r in sheets.notes_rows()]
    assert statuses == [NOTE_STATUS_USED, NOTE_STATUS_USED]


def test_no_new_notes_falls_back_to_pillars():
    sheets = FakeSheets([])  # no notes at all
    claude = FakeClaude(_payload(DAILY_DRAFT_COUNT))
    rows = generate(sheets, claude, "SYS", NOW)

    assert len(rows) == DAILY_DRAFT_COUNT
    assert len(sheets.queue_rows()) == DAILY_DRAFT_COUNT
    _, user_content = claude.calls[0]
    assert "小言なし" in user_content


def test_insufficient_notes_uses_all_new_and_backfills():
    # 2 new notes, DAILY_DRAFT_COUNT (3) drafts -> 2 used, 1 pillar-backfilled
    sheets = FakeSheets(
        [
            _note("2026-06-09T20:00", "メモA"),
            _note("2026-06-09T21:00", "メモB"),
            _note("2026-06-01T10:00", "古いメモ", status=NOTE_STATUS_USED),  # already used
        ]
    )
    claude = FakeClaude(_payload(DAILY_DRAFT_COUNT))
    rows = generate(sheets, claude, "SYS", NOW)

    assert len(rows) == DAILY_DRAFT_COUNT
    statuses = [r[NOTES_HEADER.index("status")] for r in sheets.notes_rows()]
    # both new notes -> used; previously-used stays used
    assert statuses == [NOTE_STATUS_USED, NOTE_STATUS_USED, NOTE_STATUS_USED]
    _, user_content = claude.calls[0]
    # instructs to backfill the remaining 1
    assert "残り1本" in user_content


def test_more_notes_than_count_marks_only_used_slice():
    notes = [_note(f"2026-06-09T{h:02d}:00", f"メモ{h}") for h in range(9)]  # 9 new
    sheets = FakeSheets(notes)
    claude = FakeClaude(_payload(DAILY_DRAFT_COUNT))
    generate(sheets, claude, "SYS", NOW)

    statuses = [r[NOTES_HEADER.index("status")] for r in sheets.notes_rows()]
    used = statuses.count(NOTE_STATUS_USED)
    new = statuses.count(NOTE_STATUS_NEW)
    assert used == DAILY_DRAFT_COUNT  # only the prioritized slice
    assert new == 9 - DAILY_DRAFT_COUNT  # leftovers stay new for next run
