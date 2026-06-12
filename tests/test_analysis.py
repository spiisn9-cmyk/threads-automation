"""Tests for daily analysis parsing + learnings storage."""
from __future__ import annotations

import json
from datetime import datetime

from src.core.analysis import analyze, build_analysis_user, parse_analysis
from src.core.learnings import (
    LEARNINGS_HEADER,
    LEARNINGS_SHEET,
    SOURCE_AUTO,
    append_learning,
    read_recent_learnings,
)
from src.core.queue import JST

NOW = datetime(2026, 6, 11, 7, 0, 0, tzinfo=JST)


class FakeClaude:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls: list[tuple[str, str]] = []

    def generate(self, system_prompt: str, user_content: str) -> str:
        self.calls.append((system_prompt, user_content))
        return self.payload


class FakeSheets:
    def __init__(self) -> None:
        self.sheets = {LEARNINGS_SHEET: [list(LEARNINGS_HEADER)]}

    def read_rows(self, sheet, a1):
        return [list(r) for r in self.sheets.get(sheet, [])]

    def append_row(self, sheet, row):
        self.sheets.setdefault(sheet, []).append(list(row))


def test_analyze_parses_report_and_learnings():
    payload = json.dumps(
        {
            "report_block": "暫定の振り返り（サンプル少なめ）",
            "learnings": [
                {"learning": "問いかけ型は伸びるかも", "evidence": "good評価2件"},
                {"learning": "硬い長文は伸びにくいかも", "evidence": "bad評価1件"},
            ],
        }
    )
    res = analyze(FakeClaude(payload), "SYS", [{"text": "x", "rating": "good"}])
    assert res.report_block.startswith("暫定の振り返り")
    assert len(res.learnings) == 2
    assert res.learnings[0] == ("問いかけ型は伸びるかも", "good評価2件")


def test_parse_caps_learnings_at_three():
    payload = json.dumps(
        {"report_block": "r", "learnings": [{"learning": f"l{i}"} for i in range(5)]}
    )
    assert len(parse_analysis(payload).learnings) == 3


def test_parse_handles_empty_learnings_and_fence():
    payload = "```json\n" + json.dumps({"report_block": "サンプル不足", "learnings": []}) + "\n```"
    res = parse_analysis(payload)
    assert res.learnings == []
    assert "サンプル不足" in res.report_block


def test_build_analysis_user_includes_ratings_and_text():
    user = build_analysis_user(
        [{"text": "投稿A", "views": "100", "likes": "5", "rating": "bad", "feedback": "硬い"}]
    )
    assert "rating=bad" in user
    assert "投稿A" in user


def test_build_analysis_user_empty_is_humble():
    user = build_analysis_user([])
    assert "サンプル不足" in user


def test_append_learning_writes_auto_source_and_round_trips():
    sheets = FakeSheets()
    append_learning(sheets, "学びX", "根拠Y", NOW)  # default source=auto
    rows = sheets.sheets[LEARNINGS_SHEET][1:]
    assert len(rows) == 1
    d = dict(zip(LEARNINGS_HEADER, rows[0]))
    assert d["learning"] == "学びX"
    assert d["evidence"] == "根拠Y"
    assert d["source"] == SOURCE_AUTO

    recent = read_recent_learnings(sheets)
    assert recent[-1].learning == "学びX"
    assert recent[-1].source == SOURCE_AUTO
