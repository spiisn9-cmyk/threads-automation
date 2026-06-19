"""Tests for the dashboard data layer (no Streamlit / Google needed)."""
from __future__ import annotations

from dashboard import service
from src.core.notes import NOTES_HEADER, NOTES_SHEET, NOTE_STATUS_NEW
from src.core.queue import (
    POST_QUEUE_HEADER,
    POST_QUEUE_SHEET,
    STATUS_APPROVED,
    STATUS_DRAFT,
    STATUS_POSTED,
)

POSTS_HEADER = ["post_id", "posted_at", "text", "views", "likes", "rating", "feedback"]
METRICS_HEADER = ["date", "followers", "views", "follower_delta", "note"]


class FakeSheets:
    def __init__(self, sheets: dict[str, list[list]]) -> None:
        self.sheets = sheets

    def read_rows(self, sheet, a1):
        return [list(r) for r in self.sheets.get(sheet, [])]

    def append_row(self, sheet, row):
        self.sheets.setdefault(sheet, []).append(list(row))

    def upsert_row(self, sheet, key_col, key_val, row_dict):
        grid = self.sheets[sheet]
        header = grid[0]
        key_idx = header.index(key_col)
        ordered = [row_dict.get(c, "") for c in header]
        for i, row in enumerate(grid[1:], start=1):
            if len(row) > key_idx and row[key_idx] == key_val:
                grid[i] = ordered
                return
        grid.append(ordered)

    def update_row(self, sheet, a1, row):
        idx = int(a1[1:])
        self.sheets[sheet][idx - 1] = list(row)

    def dicts(self, sheet):
        grid = self.sheets[sheet]
        return [dict(zip(grid[0], r)) for r in grid[1:]]


def _queue(rows):
    return {POST_QUEUE_SHEET: [list(POST_QUEUE_HEADER)] + rows}


def _qrow(qid, status, sched, text="本文", theme="実況・記録", posted_id="", posted_at=""):
    return [qid, sched, text, theme, status, posted_id, posted_at]


# --- normalize_scheduled_at ---

def test_normalize_scheduled_at_from_iso_tz():
    assert service.normalize_scheduled_at("2026-06-15T12:34:00+0900") == "2026-06-15 12:34"


def test_normalize_scheduled_at_passthrough_hyphen():
    assert service.normalize_scheduled_at("2026-06-15 09:05") == "2026-06-15 09:05"


def test_normalize_scheduled_at_unparsable_is_trimmed():
    assert service.normalize_scheduled_at("  あとで  ") == "あとで"


# --- drafts ---

def test_read_drafts_filters_and_sorts():
    sheets = FakeSheets(
        _queue(
            [
                _qrow("q2", STATUS_DRAFT, "2026-06-15 18:00"),
                _qrow("q1", STATUS_DRAFT, "2026-06-15 09:00"),
                _qrow("q3", STATUS_APPROVED, "2026-06-15 10:00"),
            ]
        )
    )
    drafts = service.read_drafts(sheets)
    assert [d["queue_id"] for d in drafts] == ["q1", "q2"]  # only drafts, time-sorted


def test_approve_draft_sets_status_and_preserves_theme():
    sheets = FakeSheets(_queue([_qrow("q1", STATUS_DRAFT, "2026-06-15T09:00:00+0900", theme="本音・共感")]))
    service.approve_draft(sheets, "q1", "編集後の本文", "2026-06-16 07:30")

    row = sheets.dicts(POST_QUEUE_SHEET)[0]
    assert row["status"] == STATUS_APPROVED
    assert row["text"] == "編集後の本文"
    assert row["scheduled_at"] == "2026-06-16 07:30"  # normalized hyphen form
    assert row["theme"] == "本音・共感"  # untouched column preserved


def test_save_draft_keeps_status_draft():
    sheets = FakeSheets(_queue([_qrow("q1", STATUS_DRAFT, "2026-06-15 09:00", posted_id="x")]))
    service.save_draft(sheets, "q1", "新本文", "2026-06-15 10:15")
    row = sheets.dicts(POST_QUEUE_SHEET)[0]
    assert row["status"] == STATUS_DRAFT
    assert row["text"] == "新本文"
    assert row["posted_post_id"] == "x"  # preserved


def test_read_queue_by_statuses():
    sheets = FakeSheets(
        _queue(
            [
                _qrow("q1", STATUS_DRAFT, "2026-06-15 09:00"),
                _qrow("q2", STATUS_APPROVED, "2026-06-15 10:00"),
                _qrow("q3", STATUS_POSTED, "2026-06-14 10:00"),
            ]
        )
    )
    rows = service.read_queue_by_statuses(sheets, [STATUS_APPROVED, STATUS_POSTED])
    assert [r["queue_id"] for r in rows] == ["q3", "q2"]  # sorted by scheduled_at


# --- posts / rating ---

def test_set_post_rating_updates_and_preserves_metrics():
    sheets = FakeSheets(
        {
            "posts": [
                list(POSTS_HEADER),
                ["p1", "2026-06-13", "投稿本文", "300", "20", "", ""],
            ]
        }
    )
    service.set_post_rating(sheets, "p1", "good", "問いかけが効いた")
    row = sheets.dicts("posts")[0]
    assert row["rating"] == "good"
    assert row["feedback"] == "問いかけが効いた"
    assert row["views"] == "300" and row["likes"] == "20"  # metrics preserved


def test_set_post_rating_without_feedback_keeps_existing():
    sheets = FakeSheets(
        {
            "posts": [
                list(POSTS_HEADER),
                ["p1", "2026-06-13", "本文", "10", "1", "bad", "既存メモ"],
            ]
        }
    )
    service.set_post_rating(sheets, "p1", "good")  # no feedback arg
    row = sheets.dicts("posts")[0]
    assert row["rating"] == "good"
    assert row["feedback"] == "既存メモ"  # untouched


# --- notes ---

def test_add_note_appends_new_row():
    sheets = FakeSheets({NOTES_SHEET: [list(NOTES_HEADER)]})
    service.add_note(sheets, "  今日はAPIでハマった  ", "2026-06-14")
    rows = sheets.dicts(NOTES_SHEET)
    assert len(rows) == 1
    assert rows[0] == {
        "created_at": "2026-06-14",
        "note": "今日はAPIでハマった",  # trimmed
        "theme": "",
        "status": NOTE_STATUS_NEW,
    }


def test_add_note_rejects_empty():
    sheets = FakeSheets({NOTES_SHEET: [list(NOTES_HEADER)]})
    try:
        service.add_note(sheets, "   ", "2026-06-14")
    except ValueError:
        return
    raise AssertionError("empty note should raise")


# --- metrics ---

def test_latest_metric_and_series():
    sheets = FakeSheets(
        {
            "metrics_daily": [
                list(METRICS_HEADER),
                ["2026-06-13", "27", "1000", "2", ""],
                ["2026-06-14", "30", "1500", "3", ""],
            ]
        }
    )
    latest = service.latest_metric(sheets)
    assert latest["date"] == "2026-06-14" and latest["followers"] == "30"

    series = service.metrics_series(sheets)
    assert series["dates"] == ["2026-06-13", "2026-06-14"]
    assert series["followers"] == [27, 30]
    assert series["views"] == [1000, 1500]


def test_metrics_empty_returns_none():
    sheets = FakeSheets({"metrics_daily": [list(METRICS_HEADER)]})
    assert service.latest_metric(sheets) is None
    assert service.metrics_series(sheets) == {"dates": [], "followers": [], "views": []}


def test_save_post_feedback_joins_tags_and_preserves_metrics():
    sheets = FakeSheets(
        {
            "posts": [
                POSTS_HEADER + ["tags"],
                ["p1", "2026-06-13", "本文", "300", "20", "", "", ""],
            ]
        }
    )
    service.save_post_feedback(sheets, "p1", ["問いかけ", "具体・数字"], "good", "数字が効いた")
    row = sheets.dicts("posts")[0]
    assert row["tags"] == "問いかけ | 具体・数字"  # joined
    assert row["rating"] == "good"
    assert row["feedback"] == "数字が効いた"
    assert row["views"] == "300" and row["likes"] == "20"  # preserved


def test_save_post_feedback_drops_invalid_tags():
    sheets = FakeSheets(
        {"posts": [POSTS_HEADER + ["tags"], ["p1", "2026-06-13", "本文", "1", "0", "", "", ""]]}
    )
    service.save_post_feedback(sheets, "p1", ["フック", "ニセタグ"], "ok", "")
    assert sheets.dicts("posts")[0]["tags"] == "フック"  # invalid removed


def test_save_queue_feedback_preserves_text_and_status():
    sheets = FakeSheets(_queue([_qrow("q1", STATUS_DRAFT, "2026-06-15 09:00", text="原文")]))
    service.save_queue_feedback(sheets, "q1", ["共感"], "bad", "弱い")
    row = sheets.dicts(POST_QUEUE_SHEET)[0]
    assert row["tags"] == "共感"
    assert row["rating"] == "bad"
    assert row["feedback"] == "弱い"
    assert row["status"] == STATUS_DRAFT  # not changed by feedback save
    assert row["text"] == "原文"  # not changed


def test_merge_upsert_missing_key_raises():
    sheets = FakeSheets(_queue([_qrow("q1", STATUS_DRAFT, "2026-06-15 09:00")]))
    try:
        service.approve_draft(sheets, "nope", "x", "2026-06-15 09:00")
    except ValueError:
        return
    raise AssertionError("unknown queue_id should raise")


# --- references CRUD ---

from src.core.references import REFERENCES_HEADER, REFERENCES_SHEET  # noqa: E402
from src.core.references import read_active_references  # noqa: E402


def _refs():
    return {REFERENCES_SHEET: [list(REFERENCES_HEADER)]}


def test_add_reference_appends_with_bool_flags_and_is_readable():
    sheets = FakeSheets(_refs())
    service.add_reference(
        sheets,
        source="@growth",
        text="親→返信で深掘り",
        structure_note="フックで引き→具体例→締めで問いかけ",
        is_thread=True,
        active=True,
        today="2026-06-20",
    )
    row = sheets.dicts(REFERENCES_SHEET)[0]
    assert row["source"] == "@growth"
    assert row["active"] == "TRUE" and row["is_thread"] == "TRUE"
    assert row["structure_note"].startswith("フックで引き")

    # round-trips through read_active_references (TRUE counts as active)
    active = read_active_references(sheets)
    assert len(active) == 1 and active[0].is_thread is True


def test_add_reference_rejects_empty():
    sheets = FakeSheets(_refs())
    try:
        service.add_reference(sheets, "", "", "", False, True, "2026-06-20")
    except ValueError:
        return
    raise AssertionError("empty reference should raise")


def test_set_reference_active_toggles_off_and_preserves_row():
    sheets = FakeSheets(_refs())
    service.add_reference(sheets, "@x", "本文", "メモ", False, True, "2026-06-20")
    refs = service.list_references(sheets)
    assert refs[0]["row_index"] == 2

    service.set_reference_active(sheets, refs[0]["row_index"], active=False)
    row = sheets.dicts(REFERENCES_SHEET)[0]
    assert row["active"] == "FALSE"
    assert row["source"] == "@x" and row["text"] == "本文"  # preserved
    # now excluded from active set
    assert read_active_references(sheets) == []


# --- thread-grouped draft review + bulk approve ---

def _qrow_full(**kw):
    base = {c: "" for c in POST_QUEUE_HEADER}
    base.update(kw)
    return [base[c] for c in POST_QUEUE_HEADER]


def test_read_draft_groups_groups_thread_and_singles():
    sheets = FakeSheets(
        {
            POST_QUEUE_SHEET: [list(POST_QUEUE_HEADER)]
            + [
                _qrow_full(queue_id="s", status=STATUS_DRAFT, text="単発",
                           scheduled_at="2026-06-21 09:00"),
                _qrow_full(queue_id="p", thread_id="T", seq=0, status=STATUS_DRAFT,
                           text="親", scheduled_at="2026-06-21 10:00"),
                _qrow_full(queue_id="r1", thread_id="T", seq=1, status=STATUS_DRAFT,
                           text="返信1", scheduled_at="2026-06-21 10:01"),
            ]
        }
    )
    groups = service.read_draft_groups(sheets)
    assert len(groups) == 2
    single = next(g for g in groups if not g["is_thread"])
    thread = next(g for g in groups if g["is_thread"])
    assert single["rows"][0]["queue_id"] == "s"
    assert thread["thread_id"] == "T"
    assert [r["queue_id"] for r in thread["rows"]] == ["p", "r1"]  # seq order


def test_save_rows_bulk_approves_thread():
    sheets = FakeSheets(
        {
            POST_QUEUE_SHEET: [list(POST_QUEUE_HEADER)]
            + [
                _qrow_full(queue_id="p", thread_id="T", seq=0, status=STATUS_DRAFT,
                           text="親", scheduled_at="2026-06-21T10:00:00+0900"),
                _qrow_full(queue_id="r1", thread_id="T", seq=1, status=STATUS_DRAFT,
                           text="返信1", scheduled_at="2026-06-21T10:01:00+0900"),
            ]
        }
    )
    service.save_rows(
        sheets,
        [
            {"queue_id": "p", "text": "親(編集)", "scheduled_at": "2026-06-22 07:30"},
            {"queue_id": "r1", "text": "返信1(編集)"},
        ],
        approve=True,
    )
    by = {d["queue_id"]: d for d in sheets.dicts(POST_QUEUE_SHEET)}
    assert by["p"]["status"] == STATUS_APPROVED and by["r1"]["status"] == STATUS_APPROVED
    assert by["p"]["text"] == "親(編集)" and by["r1"]["text"] == "返信1(編集)"
    assert by["p"]["scheduled_at"] == "2026-06-22 07:30"  # normalized
    assert by["p"]["thread_id"] == "T" and by["r1"]["seq"] == 1  # preserved
