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

from src.utils.logging_setup import setup_logging

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

SHEETS: dict[str, list[str]] = {
    "metrics_daily": ["date", "followers", "views", "follower_delta", "note"],
    "posts": ["post_id", "posted_at", "text", "views", "likes"],
    "logs": ["datetime", "job", "status", "count", "message"],
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
        if current and current[0]:
            log.info("Header already present for %s, skipping", title)
            continue
        spreadsheets.values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{title}!A1",
            valueInputOption="RAW",
            body={"values": [header]},
        ).execute()
        log.info("Wrote header for %s", title)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
