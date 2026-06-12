"""Shared definitions and helpers for the `references` sheet (swipe file).

A "reference" is an example of a high-performing post that うに wants to learn
the *structure* from — the hook, framing, angle, and how it poses a question —
NOT the topic or wording. generate_drafts feeds active references to the model
purely as form/structure examples, with an explicit no-copy instruction.
Pure stdlib so init_sheets can import it without heavy dependencies.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from src.core.queue import rows_to_dicts

REFERENCES_SHEET = "references"
REFERENCES_HEADER = ["created_at", "source", "impressions", "text", "learn", "active"]

REF_ACTIVE = "active"
REF_OFF = "off"


@dataclass(frozen=True)
class Reference:
    """One swipe-file entry to learn post structure from."""

    source: str
    impressions: str
    text: str
    learn: str


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
        if not text:
            continue
        out.append(
            Reference(
                source=str(d.get("source", "")).strip(),
                impressions=str(d.get("impressions", "")).strip(),
                text=text,
                learn=str(d.get("learn", "")).strip(),
            )
        )
    return out
