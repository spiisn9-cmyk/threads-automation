"""Application settings, loaded exclusively from environment variables.

Secrets are NEVER hardcoded. Locally they come from a .env file (via
python-dotenv); in production GitHub Actions injects them as env vars.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv optional in CI where env is injected
    pass


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
