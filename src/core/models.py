"""Immutable domain models.

All dataclasses are frozen — never mutate an instance, build a new one with
dataclasses.replace() or by constructing a fresh object.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DailyMetric:
    """A single day's account-level snapshot."""

    date: str  # "YYYY-MM-DD"
    followers: int
    views: int
    follower_delta: int | None = None
    note: str = ""


@dataclass(frozen=True)
class PostMetric:
    """Per-post insights for a single Threads media item."""

    post_id: str
    posted_at: str
    text: str
    views: int
    likes: int
