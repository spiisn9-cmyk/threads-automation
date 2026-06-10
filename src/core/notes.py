"""Shared definitions and helpers for the `notes` sheet (小言メモ→投稿素材).

A "note" is a quick jot from the author (うに). generate_drafts turns unused
notes into polished drafts and flips them to status=used.
Pure stdlib so init_sheets can import it without heavy dependencies.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

NOTES_SHEET = "notes"
NOTES_HEADER = ["created_at", "note", "theme", "status"]

NOTE_STATUS_NEW = "new"
NOTE_STATUS_USED = "used"


@dataclass(frozen=True)
class NewNote:
    """An unused note, with its 1-based sheet row so status can be updated."""

    row_index: int
    created_at: str
    note: str
    theme: str


class _NotesSheet(Protocol):
    def read_rows(self, sheet: str, a1: str) -> list[list[Any]]: ...

    def update_row(self, sheet: str, a1: str, row: list[Any]) -> None: ...


def read_new_notes(sheets: _NotesSheet) -> list[NewNote]:
    """Return notes with status==new, in sheet order (oldest first)."""
    rows = sheets.read_rows(NOTES_SHEET, "A1:ZZ")
    if not rows:
        return []
    header = [str(c) for c in rows[0]]
    idx = {col: i for i, col in enumerate(header)}

    def cell(row: list[Any], col: str) -> str:
        i = idx.get(col)
        return str(row[i]) if i is not None and i < len(row) else ""

    out: list[NewNote] = []
    for offset, row in enumerate(rows[1:], start=2):  # row 1 is the header
        if cell(row, "status").strip() == NOTE_STATUS_NEW:
            out.append(
                NewNote(
                    row_index=offset,
                    created_at=cell(row, "created_at"),
                    note=cell(row, "note"),
                    theme=cell(row, "theme"),
                )
            )
    return out


def mark_note_used(sheets: _NotesSheet, note: NewNote) -> None:
    """Flip a note's status to used, preserving its other columns."""
    sheets.update_row(
        NOTES_SHEET,
        f"A{note.row_index}",
        [note.created_at, note.note, note.theme, NOTE_STATUS_USED],
    )
