"""Technique / structure tags for post feedback.

Edit TECHNIQUE_TAGS to add or rename tags — everything else (validation,
dashboard multiselect, learning-loop signals) derives from this flat list.
Tags are stored in a single cell joined by " | " and split back on read.
Pure stdlib so any module can import it freely.
"""
from __future__ import annotations

from typing import Any, Iterable

# Flat list of hook / structure techniques (order matters for display).
TECHNIQUE_TAGS: list[str] = [
    "フック",
    "共感",
    "問いかけ",
    "ストーリー",
    "具体・数字",
    "本音・弱み開示",
    "学び・気づき",
    "ギャップ・意外性",
    "オチ・締め",
    "CTA",
    "リスト・まとめ",
    "例え・比喩",
]

_VALID = set(TECHNIQUE_TAGS)
TAG_SEPARATOR = " | "


def normalize_tags(tags: Iterable[Any]) -> list[str]:
    """Keep only known tags, de-duplicated, in TECHNIQUE_TAGS order-stable input order."""
    seen: set[str] = set()
    out: list[str] = []
    for t in tags or []:
        s = str(t).strip()
        if s in _VALID and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def split_tags(value: Any) -> list[str]:
    """Split a stored ' | '-joined cell into a clean list (drops blanks)."""
    if not value:
        return []
    return [p.strip() for p in str(value).split("|") if p.strip()]


def parse_tags(value: Any) -> list[str]:
    """Read a stored cell into a list of VALID tags (safe for multiselect default)."""
    return normalize_tags(split_tags(value))


def join_tags(tags: Iterable[Any]) -> str:
    """Serialize tags to a ' | '-joined cell, dropping unknown/duplicate tags."""
    return TAG_SEPARATOR.join(normalize_tags(tags))
