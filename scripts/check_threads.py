"""Standalone smoke test for the Threads API client.

Run this FIRST — before Sheets / Claude / email are configured — to confirm
that account numbers and recent posts can be fetched with just the access
token. It prints the parsed numbers and logs the RAW API responses so you can
verify the real JSON shape.

Usage (from the repo root):
    python -m scripts.check_threads
"""
from __future__ import annotations

import logging
import os
import sys

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv optional; env may be exported directly
    pass

from src.clients.threads_client import ThreadsClient
from src.utils.logging_setup import setup_logging


def main() -> int:
    setup_logging(logging.INFO)
    log = logging.getLogger("check_threads")

    token = os.environ.get("THREADS_ACCESS_TOKEN")
    if not token:
        log.error("THREADS_ACCESS_TOKEN is not set (use .env or `export` it)")
        return 1

    # Confirmed spec uses /me, so THREADS_USER_ID is not strictly required here.
    with ThreadsClient(access_token=token, user_id="me") as client:
        insights = client.get_account_insights()
        print("=== account insights ===")
        print(f"followers_count = {insights.followers}")
        print(f"views           = {insights.views}")

        media = client.list_recent_media(limit=5)
        print(f"\n=== recent posts ({len(media)}) ===")
        for item in media:
            stats = client.get_media_insights(item["id"])
            preview = (item["text"] or "").replace("\n", " ")[:40]
            print(
                f"[{item['id']}] views={stats['views']} "
                f"likes={stats['likes']} posted={item['timestamp']} {preview!r}"
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
