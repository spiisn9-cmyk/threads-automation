"""Shared definitions and helpers for the `learnings` sheet (learning loop).

A "learning" is a short, evidence-backed takeaway about what works / what to
avoid in うに's posts. The daily analysis appends auto-derived learnings
(source=auto); うに can also add her own (source=uni). generate_drafts reads
recent learnings to steer drafts toward what works and away from what doesn't.
Pure stdlib so init_sheets can import it without heavy dependencies.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from src.core.queue import JST, rows_to_dicts

LEARNINGS_SHEET = "learnings"
LEARNINGS_HEADER = ["created_at", "learning", "evidence", "source"]

SOURCE_AUTO = "auto"
SOURCE_UNI = "uni"


@dataclass(frozen=True)
class Learning:
    created_at: str
    learning: str
    evidence: str
    source: str


class _LearningsSheet(Protocol):
    def read_rows(self, sheet: str, a1: str) -> list[list[Any]]: ...

    def append_row(self, sheet: str, row: list[Any]) -> None: ...


def read_recent_learnings(sheets: _LearningsSheet, limit: int = 10) -> list[Learning]:
    """Most recent learnings (oldest..newest within the returned window)."""
    rows = sheets.read_rows(LEARNINGS_SHEET, "A1:ZZ")
    items: list[Learning] = []
    for d in rows_to_dicts(rows):
        text = str(d.get("learning", "")).strip()
        if not text:
            continue
        items.append(
            Learning(
                created_at=str(d.get("created_at", "")),
                learning=text,
                evidence=str(d.get("evidence", "")).strip(),
                source=str(d.get("source", "")).strip(),
            )
        )
    items.sort(key=lambda l: l.created_at)
    return items[-limit:] if limit else items


def append_learning(
    sheets: _LearningsSheet,
    learning: str,
    evidence: str,
    now: datetime,
    source: str = SOURCE_AUTO,
) -> None:
    created = now.astimezone(JST).strftime("%Y-%m-%dT%H:%M:%S%z")
    sheets.append_row(LEARNINGS_SHEET, [created, learning, evidence, source])
