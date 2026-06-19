"""Application settings, loaded exclusively from environment variables.

Secrets are NEVER hardcoded. Locally they come from a .env file (via
python-dotenv); in production GitHub Actions injects them as env vars.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta, timezone

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv optional in CI where env is injected
    pass

# --- Phase 2 constants (draft generation & scheduled publishing) ---
JST = timezone(timedelta(hours=9))

# How many drafts to generate per weekly run (one per day, starting tomorrow).
DRAFT_COUNT = 7
# Default publish time-of-day for generated drafts (JST). Editable per row later.
DRAFT_POST_HOUR = 12
DRAFT_POST_MINUTE = 0
# How many top-performing posts to feed the model as context.
DRAFT_TOP_POSTS = 5

# --- Daily learning loop ---
DAILY_DRAFT_COUNT = 3  # candidate drafts generated per daily run
ANALYSIS_LOOKBACK_DAYS = 7  # how many days of posts to analyze each morning

# --- Publishing safety guards (anti-ban; human-like, conservative pace) ---
# This tool posts via the official Threads API (threads_content_publish).
MAX_POSTS_PER_RUN = 1  # at most 1 post per invocation (physically prevents bursts)
MAX_POSTS_PER_DAY = 3  # at most 3 posts per JST calendar day
MIN_HOURS_BETWEEN_POSTS = 0  # 0 = no minimum gap between posts
POST_WINDOW_START_HOUR = 8  # earliest publish hour (JST), inclusive
POST_WINDOW_END_HOUR = 22  # latest publish hour (JST), exclusive
POST_JITTER_MINUTES = 3  # random 0..N min delay before posting to scatter timing
# Kept short on purpose: a long sleep can outlive the Sheets HTTP connection.
# The SheetsClient also retries+reconnects, so timing scatter no longer risks
# a stale-connection failure on the post-sleep status write.

# Wait for the media container to become status=FINISHED before publishing
# (Meta returns 400 "media not ready" if you publish a still-processing container).
PUBLISH_STATUS_POLL_SECONDS = 3  # interval between status checks
PUBLISH_STATUS_MAX_CHECKS = 20  # ~60s total before giving up

# Threads (連投) publishing.
# Default False: post the parent automatically; replies post on later runs once
# their predecessor is published (manual/cross-run, fully guard-respecting).
# True: publish the whole approved chain in one run with a short inter-reply
# delay (a thread = one per-run unit).
PUBLISH_THREAD_REPLIES_INLINE = False
THREAD_REPLY_DELAY_SECONDS = 30  # short gap between chained replies (inline mode)


class SettingsError(RuntimeError):
    """Raised when a required setting is missing or malformed."""


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SettingsError(f"Required environment variable '{name}' is not set")
    return value


def _optional(name: str, default: str) -> str:
    return os.environ.get(name) or default


def _optional_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise SettingsError(
            f"Environment variable '{name}' must be an integer, got {raw!r}"
        ) from exc


@dataclass(frozen=True)
class Settings:
    threads_access_token: str
    threads_user_id: str
    google_sa_json: str
    spreadsheet_id: str
    anthropic_api_key: str
    claude_model: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_pass: str
    mail_to: str
    report_lookback_days: int


def load_settings() -> Settings:
    """Build a frozen Settings from the environment, validating as we go."""
    return Settings(
        threads_access_token=_require("THREADS_ACCESS_TOKEN"),
        threads_user_id=_require("THREADS_USER_ID"),
        google_sa_json=_require("GOOGLE_SA_JSON"),
        spreadsheet_id=_require("SPREADSHEET_ID"),
        anthropic_api_key=_require("ANTHROPIC_API_KEY"),
        claude_model=_optional("CLAUDE_MODEL", "claude-sonnet-4-6"),
        smtp_host=_require("SMTP_HOST"),
        smtp_port=_optional_int("SMTP_PORT", 587),
        smtp_user=_require("SMTP_USER"),
        smtp_pass=_require("SMTP_PASS"),
        mail_to=_require("MAIL_TO"),
        report_lookback_days=_optional_int("REPORT_LOOKBACK_DAYS", 7),
    )
