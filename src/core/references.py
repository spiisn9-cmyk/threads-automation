"""Shared definitions and helpers for the `references` sheet (swipe file).

A "reference" is an example post うに wants to learn the *structure* from — the
hook, framing, angle, how it poses a question, and (for threads) how the parent
and replies are組み立て. generate_drafts feeds active references to the model
purely as structure examples, with an explicit no-copy instruction: never copy
the body / topic / specific wording.

Schema note: `structure_note` and `is_thread` were added later; the older
`impressions` / `learn` columns are kept for backward-compatibility. If
`structure_note` is blank, `learn` is used as a fallback.
Pure stdlib so init_sheets can import it without heavy dependencies.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from src.core.queue import rows_to_dicts

REFERENCES_SHEET = "references"
REFERENCES_HEADER = [
    "created_at",
    "source",
    "impressions",  # kept for back-compat
    "text",  # learning-only sample (never copied verbatim)
    "learn",  # kept for back-compat (fallback for structure_note)
    "active",
    "structure_note",  # 型・フック・ツリーの組み立てメモ
    "is_thread",  # ツリー(連投)の例か
]

REF_ACTIVE = "active"
REF_OFF = "off"

_TRUE_VALUES = {"true", "yes", "y", "1", "thread", "tree", "ツリー", "連投"}


def _as_bool(value: Any) -> bool:
    return str(value or "").strip().lower() in _TRUE_VALUES


@dataclass(frozen=True)
class Reference:
    """One swipe-file entry to learn post structure (and thread shape) from."""

    source: str
    text: str
    structure_note: str
    is_thread: bool
    impressions: str = ""


class _RefsSheet(Protocol):
    def read_rows(self, sheet: str, a1: str) -> list[list[Any]]: ...


def read_active_references(sheets: _RefsSheet) -> list[Reference]:
    """Return references whose `active` column is "active" (case-insensitive)."""
    rows = sheets.read_rows(REFERENCES_SHEET, "A1:ZZ")
    out: list[Reference] = []
    for d in rows_to_dicts(rows):
        if str(d.get("active", "")).strip().lower() != REF_ACTIVE:
            continue
        text = str(d.get("text", "")).strip()
        structure_note = str(d.get("structure_note", "")).strip() or str(
            d.get("learn", "")
        ).strip()
        # Keep a reference if it has anything useful to learn from.
        if not text and not structure_note:
            continue
        out.append(
            Reference(
                source=str(d.get("source", "")).strip(),
                text=text,
                structure_note=structure_note,
                is_thread=_as_bool(d.get("is_thread")),
                impressions=str(d.get("impressions", "")).strip(),
            )
        )
    return out
