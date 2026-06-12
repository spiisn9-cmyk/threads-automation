"""Create the metrics_daily / posts / logs sheets with header rows.

Idempotent: existing sheets are left in place, and a header is written only
when row 1 is empty.

Usage (from the repo root):
    python -m scripts.init_sheets
"""
from __future__ import annotations

import json
import logging
import os

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from google.oauth2 import service_account
from googleapiclient.discovery import build

from src.core.learnings import LEARNINGS_HEADER, LEARNINGS_SHEET
from src.core.notes import NOTES_HEADER, NOTES_SHEET
from src.core.queue import POST_QUEUE_HEADER, POST_QUEUE_SHEET
from src.core.references import REFERENCES_HEADER, REFERENCES_SHEET
from src.utils.logging_setup import setup_logging

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

SHEETS: dict[str, list[str]] = {
    "metrics_daily": ["date", "followers", "views", "follower_delta", "note"],
    # rating (good/ok/bad) and feedback are human-entered; appended at the end.
    "posts": ["post_id", "posted_at", "text", "views", "likes", "rating", "feedback"],
    "logs": ["datetime", "job", "status", "count", "message"],
    # Phase 2: scheduled post queue (created only if missing; existing sheets untouched).
    POST_QUEUE_SHEET: POST_QUEUE_HEADER,
    # 小言メモ→投稿素材 (created only if missing).
    NOTES_SHEET: NOTES_HEADER,
    # 参考資料（伸びている投稿の型を学ぶ swipe file）(created only if missing).
    REFERENCES_SHEET: REFERENCES_HEADER,
    # 日々の学び（分析の蓄積）(created only if missing).
    LEARNINGS_SHEET: LEARNINGS_HEADER,
}


def main() -> int:
    setup_logging()
    log = logging.getLogger("init_sheets")

    sa_json = os.environ.get("GOOGLE_SA_JSON")
    spreadsheet_id = os.environ.get("SPREADSHEET_ID")
    if not sa_json or not spreadsheet_id:
        log.error("GOOGLE_SA_JSON and SPREADSHEET_ID must both be set")
        return 1

    creds = service_account.Credentials.from_service_account_info(
        json.loads(sa_json), scopes=SCOPES
    )
    spreadsheets = build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()

    meta = spreadsheets.get(spreadsheetId=spreadsheet_id).execute()
    existing = {s["properties"]["title"] for s in meta.get("sheets", [])}

    add_requests = [
        {"addSheet": {"properties": {"title": title}}}
        for title in SHEETS
        if title not in existing
    ]
    if add_requests:
        spreadsheets.batchUpdate(
            spreadsheetId=spreadsheet_id, body={"requests": add_requests}
        ).execute()
        created = [r["addSheet"]["properties"]["title"] for r in add_requests]
        log.info("Created sheets: %s", created)
    else:
        log.info("All target sheets already exist")

    for title, header in SHEETS.items():
        current = (
            spreadsheets.values()
            .get(spreadsheetId=spreadsheet_id, range=f"{title}!1:1")
            .execute()
            .get("values", [])
        )
        existing_header = current[0] if current else []

        if not existing_header:
            spreadsheets.values().update(
                spreadsheetId=spreadsheet_id,
                range=f"{title}!A1",
                valueInputOption="RAW",
                body={"values": [header]},
            ).execute()
            log.info("Wrote header for %s", title)
            continue

        # Append any missing columns at the END (positional data stays intact).
        missing = [c for c in header if c not in existing_header]
        if not missing:
            log.info("Header already present for %s, skipping", title)
            continue
        new_header = list(existing_header) + missing
        spreadsheets.values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{title}!A1",
            valueInputOption="RAW",
            body={"values": [new_header]},
        ).execute()
        log.info("Added columns %s to %s", missing, title)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
