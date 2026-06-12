"""Google Sheets client (resilient to stale connections).

Authenticates with a service-account JSON string (the literal contents of the
key file, passed via GOOGLE_SA_JSON) and exposes append / read / upsert
helpers scoped to the spreadsheets scope.

google-api-python-client uses httplib2, which reuses a single HTTP connection.
After a long idle (e.g. the publish job's jitter sleep) that connection can be
silently closed by the server, so the next call fails with a transport error.
Every API call therefore goes through `_execute_with_retry`, which retries
transient errors and REBUILDS the service (fresh credentials + connection)
between attempts. Errors are re-raised with the underlying cause attached.
"""
from __future__ import annotations

import http.client
import json
import logging
import ssl
import time
from typing import Any, Callable

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# HTTP statuses worth retrying.
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
# Transport-level failures (stale/closed sockets, TLS resets, timeouts).
# OSError covers ConnectionError, BrokenPipeError, TimeoutError, socket.error.
_TRANSPORT_ERRORS = (OSError, ssl.SSLError, http.client.HTTPException)

_MAX_ATTEMPTS = 4
_BASE_DELAY = 1.0


def _is_transient_sheets_error(exc: BaseException) -> bool:
    if isinstance(exc, HttpError):
        status = getattr(getattr(exc, "resp", None), "status", None)
        try:
            return int(status) in _RETRYABLE_STATUS
        except (TypeError, ValueError):
            return False
    return isinstance(exc, _TRANSPORT_ERRORS)


def _execute_with_retry(
    make_request: Callable[[], Any],
    rebuild_fn: Callable[[], None],
    *,
    op: str,
    max_attempts: int = _MAX_ATTEMPTS,
    sleep_fn: Callable[[float], None] = time.sleep,
    base_delay: float = _BASE_DELAY,
) -> Any:
    """Execute a Sheets request, retrying transient errors after rebuilding.

    `make_request` must build the request from the CURRENT service each call, so
    that a rebuild (new connection) takes effect on the next attempt.
    Non-transient errors are raised immediately; transient ones retry up to
    `max_attempts`, rebuilding the connection in between.
    """
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return make_request().execute()
        except Exception as exc:
            if not _is_transient_sheets_error(exc):
                raise
            last_exc = exc
            if attempt == max_attempts:
                logger.error(
                    "Sheets %s failed after %d attempts: %r", op, attempt, exc
                )
                raise
            logger.warning(
                "Sheets %s transient error (attempt %d/%d): %r — rebuilding connection",
                op,
                attempt,
                max_attempts,
                exc,
            )
            try:
                rebuild_fn()
            except Exception as rebuild_exc:
                logger.warning("Sheets reconnect failed: %r", rebuild_exc)
            sleep_fn(base_delay * attempt)
    assert last_exc is not None  # unreachable; loop either returns or raises
    raise last_exc


class SheetsClient:
    def __init__(self, sa_json: str, spreadsheet_id: str) -> None:
        if not sa_json:
            raise ValueError("sa_json is required")
        if not spreadsheet_id:
            raise ValueError("spreadsheet_id is required")

        try:
            self._sa_info = json.loads(sa_json)
        except json.JSONDecodeError as exc:
            raise ValueError("GOOGLE_SA_JSON is not valid JSON") from exc

        self._spreadsheet_id = spreadsheet_id
        self._build()

    def _build(self) -> None:
        """(Re)build credentials + service — gives a fresh HTTP connection."""
        try:
            creds = service_account.Credentials.from_service_account_info(
                self._sa_info, scopes=SCOPES
            )
            self._service = build(
                "sheets", "v4", credentials=creds, cache_discovery=False
            )
        except Exception as exc:
            logger.error("Failed to authenticate to Google Sheets: %s", exc)
            raise RuntimeError("Could not authenticate to Google Sheets") from exc

    def _values(self):
        return self._service.spreadsheets().values()

    def append_row(self, sheet: str, row: list[Any]) -> None:
        def make_request():
            return self._values().append(
                spreadsheetId=self._spreadsheet_id,
                range=f"{sheet}!A1",
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body={"values": [row]},
            )

        try:
            _execute_with_retry(make_request, self._build, op=f"append '{sheet}'")
        except Exception as exc:
            logger.error("append_row failed for sheet %r: %r", sheet, exc)
            raise RuntimeError(
                f"Failed to append row to '{sheet}': {type(exc).__name__}: {exc}"
            ) from exc

    def read_rows(self, sheet: str, a1: str) -> list[list[Any]]:
        def make_request():
            return self._values().get(
                spreadsheetId=self._spreadsheet_id,
                range=f"{sheet}!{a1}",
            )

        try:
            resp = _execute_with_retry(make_request, self._build, op=f"read '{sheet}'")
        except Exception as exc:
            logger.error("read_rows failed for %r!%s: %r", sheet, a1, exc)
            raise RuntimeError(
                f"Failed to read rows from '{sheet}': {type(exc).__name__}: {exc}"
            ) from exc
        return resp.get("values", [])

    def update_row(self, sheet: str, a1: str, row: list[Any]) -> None:
        def make_request():
            return self._values().update(
                spreadsheetId=self._spreadsheet_id,
                range=f"{sheet}!{a1}",
                valueInputOption="USER_ENTERED",
                body={"values": [row]},
            )

        try:
            _execute_with_retry(make_request, self._build, op=f"update '{sheet}'")
        except Exception as exc:
            logger.error("update_row failed for %r!%s: %r", sheet, a1, exc)
            raise RuntimeError(
                f"Failed to update row in '{sheet}': {type(exc).__name__}: {exc}"
            ) from exc

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
                self.update_row(sheet, f"A{offset}", ordered_row)
                logger.info("upsert (update) %s=%s in %s row %d", key_col, key_val, sheet, offset)
                return

        self.append_row(sheet, ordered_row)
        logger.info("upsert (append) %s=%s in %s", key_col, key_val, sheet)
