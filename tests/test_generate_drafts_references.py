"""Tests for the references (swipe file) feature in generate_drafts."""
from __future__ import annotations

import json
from datetime import datetime

from config.settings import DAILY_DRAFT_COUNT
from src.core.queue import JST, POST_QUEUE_HEADER, POST_QUEUE_SHEET
from src.core.references import REFERENCES_HEADER, REFERENCES_SHEET
from src.jobs.generate_drafts import generate

NOW = datetime(2026, 6, 10, 9, 0, 0, tzinfo=JST)


class FakeSheets:
    def __init__(self, references: list[list]) -> None:
        self.sheets: dict[str, list[list]] = {
            "posts": [["post_id", "posted_at", "text", "views", "likes"]],
            REFERENCES_SHEET: [list(REFERENCES_HEADER)] + references,
            POST_QUEUE_SHEET: [list(POST_QUEUE_HEADER)],
        }

    def read_rows(self, sheet, a1):
        return [list(r) for r in self.sheets.get(sheet, [])]

    def append_row(self, sheet, row):
        self.sheets.setdefault(sheet, []).append(list(row))

    def update_row(self, sheet, a1, row):
        idx = int(a1[1:])
        self.sheets[sheet][idx - 1] = list(row)


class FakeClaude:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls: list[tuple[str, str]] = []

    def generate(self, system_prompt: str, user_content: str) -> str:
        self.calls.append((system_prompt, user_content))
        return self.payload


def _payload(n: int) -> str:
    return json.dumps([{"theme": "学び・気づき", "text": f"本文{i}"} for i in range(n)])


def test_active_references_are_passed_as_form_examples():
    sheets = FakeSheets(
        [
            ["2026-06-01", "@growth_guy", "12000", "結局、最初の一歩って何だっけ？", "問いかけで始める型", "active"],
            ["2026-06-02", "@off_example", "9000", "これはoff", "使わない", "off"],
        ]
    )
    claude = FakeClaude(_payload(DAILY_DRAFT_COUNT))
    generate(sheets, claude, "SYS", NOW)
    _, user_content = claude.calls[0]

    # the active reference is surfaced as a structure example...
    assert "参考資料" in user_content
    assert "結局、最初の一歩って何だっけ？" in user_content
    assert "@growth_guy" in user_content
    # ...with an explicit no-copy instruction...
    assert "丸写し" in user_content
    # ...and the off reference is NOT included
    assert "これはoff" not in user_content


def test_no_active_references_keeps_former_behavior():
    sheets = FakeSheets([])  # references sheet empty
    claude = FakeClaude(_payload(DAILY_DRAFT_COUNT))
    rows = generate(sheets, claude, "SYS", NOW)
    _, user_content = claude.calls[0]

    assert len(rows) == DAILY_DRAFT_COUNT
    # no reference section at all
    assert "参考資料" not in user_content


def test_off_only_references_show_no_section():
    sheets = FakeSheets(
        [["2026-06-01", "@x", "100", "オフのみ", "", "off"]]
    )
    claude = FakeClaude(_payload(DAILY_DRAFT_COUNT))
    generate(sheets, claude, "SYS", NOW)
    _, user_content = claude.calls[0]
    assert "参考資料" not in user_content
    assert "オフのみ" not in user_content


def test_thread_reference_adds_tree_candidate_instruction():
    # full 8-col row: created_at, source, impressions, text, learn, active,
    # structure_note, is_thread
    sheets = FakeSheets(
        [
            [
                "2026-06-01", "@thready", "", "親→返信で深掘りする連投例",
                "", "active", "親フックで引き→返信で具体例→締めで問いかけ", "true",
            ]
        ]
    )
    claude = FakeClaude(_payload(DAILY_DRAFT_COUNT))
    generate(sheets, claude, "SYS", NOW)
    _, uc = claude.calls[0]
    assert "参考資料" in uc
    assert "丸写し" in uc  # no-copy instruction present
    assert "親フックで引き→返信で具体例→締めで問いかけ" in uc  # structure_note surfaced
    assert "ツリー候補" in uc  # thread-candidate guidance triggered
    assert "thread" in uc  # tells the model how to emit a thread
