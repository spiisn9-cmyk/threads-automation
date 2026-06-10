"""Google Sheets client.

Authenticates with a service-account JSON string (the literal contents of the
key file, passed via GOOGLE_SA_JSON) and exposes append / read / upsert
helpers scoped to the spreadsheets scope.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


class SheetsClient:
    def __init__(self, sa_json: str, spreadsheet_id: str) -> None:
        if not sa_json:
            raise ValueError("sa_json is required")
        if not spreadsheet_id:
            raise ValueError("spreadsheet_id is required")

        try:
            info = json.loads(sa_json)
        except json.JSONDecodeError as exc:
            raise ValueError("GOOGLE_SA_JSON is not valid JSON") from exc

        try:
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=SCOPES
            )
            service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        except Exception as exc:
            logger.error("Failed to authenticate to Google Sheets: %s", exc)
            raise RuntimeError("Could not authenticate to Google Sheets") from exc

        self._spreadsheet_id = spreadsheet_id
        self._values = service.spreadsheets().values()

    def append_row(self, sheet: str, row: list[Any]) -> None:
        try:
            self._values.append(
                spreadsheetId=self._spreadsheet_id,
                range=f"{sheet}!A1",
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body={"values": [row]},
            ).execute()
        except Exception as exc:
            logger.error("append_row failed for sheet %r: %s", sheet, exc)
            raise RuntimeError(f"Failed to append row to '{sheet}'") from exc

    def read_rows(self, sheet: str, a1: str) -> list[list[Any]]:
        try:
            resp = self._values.get(
                spreadsheetId=self._spreadsheet_id,
                range=f"{sheet}!{a1}",
            ).execute()
        except Exception as exc:
            logger.error("read_rows failed for %r!%s: %s", sheet, a1, exc)
            raise RuntimeError(f"Failed to read rows from '{sheet}'") from exc
        return resp.get("values", [])

    def _update_row(self, sheet: str, a1: str, row: list[Any]) -> None:
        try:
            self._values.update(
                spreadsheetId=self._spreadsheet_id,
                range=f"{sheet}!{a1}",
                valueInputOption="USER_ENTERED",
                body={"values": [row]},
            ).execute()
        except Exception as exc:
            logger.error("update_row failed for %r!%s: %s", sheet, a1, exc)
            raise RuntimeError(f"Failed to update row in '{sheet}'") from exc

    def upsert_row(
        self, sheet: str, key_col: str, key_val: str, row_dict: dict[str, Any]
    ) -> None:
        """Insert or overwrite a row keyed by a column value.

        The row is laid out in the sheet's existing header order, so callers
        never depend on dict ordering. If a row with key_col == key_val exists
        it is overwritten in place; otherwise a new row is appended.
        """
        existing = self.read_rows(sheet, "A1:ZZ")
        if not existing:
            raise RuntimeError(
                f"Sheet '{sheet}' has no header row; run scripts/init_sheets.py first"
            )

        header = existing[0]
        if key_col not in header:
            raise RuntimeError(
                f"Key column '{key_col}' not found in '{sheet}' header {header}"
            )
        key_idx = header.index(key_col)
        ordered_row = [row_dict.get(col, "") for col in header]

        for offset, row in enumerate(existing[1:], start=2):  # A2 is the first data row
            if len(row) > key_idx and row[key_idx] == key_val:
                self._update_row(sheet, f"A{offset}", ordered_row)
                logger.info("upsert (update) %s=%s in %s row %d", key_col, key_val, sheet, offset)
                return

        self.append_row(sheet, ordered_row)
        logger.info("upsert (append) %s=%s in %s", key_col, key_val, sheet)
