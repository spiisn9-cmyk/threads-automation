"""Tests for idempotent daily upsert and follower_delta computation."""
from __future__ import annotations

from src.core.models import DailyMetric
from src.core.upsert import upsert_daily


class FakeSheets:
    """In-memory stand-in mirroring SheetsClient.upsert_row semantics."""

    def __init__(self, header: list[str]) -> None:
        self.rows: list[list[object]] = [list(header)]

    def read_rows(self, sheet: str, a1: str) -> list[list[object]]:
        return [list(r) for r in self.rows]

    def upsert_row(self, sheet, key_col, key_val, row_dict):
        header = self.rows[0]
        key_idx = header.index(key_col)
        ordered = [row_dict.get(col, "") for col in header]
        for i, row in enumerate(self.rows[1:], start=1):
            if len(row) > key_idx and row[key_idx] == key_val:
                self.rows[i] = ordered
                return
        self.rows.append(ordered)


HEADER = ["date", "followers", "views", "follower_delta", "note"]


def test_same_date_upsert_does_not_duplicate():
    sheets = FakeSheets(HEADER)
    upsert_daily(sheets, DailyMetric(date="2026-06-10", followers=27, views=100))
    upsert_daily(sheets, DailyMetric(date="2026-06-10", followers=30, views=150))

    data_rows = sheets.rows[1:]
    assert len(data_rows) == 1, "same date must overwrite, not duplicate"
    assert data_rows[0][1] == 30, "latest followers value should win"
    assert data_rows[0][2] == 150, "latest views value should win"


def test_follower_delta_computed_from_previous_day():
    sheets = FakeSheets(HEADER)
    upsert_daily(sheets, DailyMetric(date="2026-06-09", followers=20, views=50))
    written = upsert_daily(sheets, DailyMetric(date="2026-06-10", followers=27, views=100))

    assert written.follower_delta == 7


def test_follower_delta_none_on_first_day():
    sheets = FakeSheets(HEADER)
    written = upsert_daily(sheets, DailyMetric(date="2026-06-09", followers=20, views=50))
    assert written.follower_delta is None


def test_follower_delta_uses_most_recent_earlier_day():
    sheets = FakeSheets(HEADER)
    upsert_daily(sheets, DailyMetric(date="2026-06-01", followers=10, views=10))
    upsert_daily(sheets, DailyMetric(date="2026-06-09", followers=20, views=50))
    written = upsert_daily(sheets, DailyMetric(date="2026-06-10", followers=27, views=100))
    # delta is vs 2026-06-09 (20), not 2026-06-01 (10)
    assert written.follower_delta == 7
