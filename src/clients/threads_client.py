"""Threads (Meta) Graph API client.

Confirmed against a live account. Base: https://graph.threads.net/v1.0

Endpoints used (and ONLY these — no competing/duplicate fetch methods):
  - GET /me/threads_insights?metric=views,followers_count   -> account numbers
  - GET /me/threads?fields=id,text,timestamp&limit=N        -> recent posts
  - GET /{media-id}/insights?metric=views,likes             -> per-post numbers

Parsing is defensive on purpose. The exact JSON shape can vary:
  - account/post metrics usually arrive as total_value.value
  - `views` may instead arrive as a time series under values[].value
Both shapes are handled, and a missing key yields None rather than crashing.
On the first run the RAW response is logged so the real shape can be confirmed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://graph.threads.net/v1.0"
REQUEST_TIMEOUT = 30.0


def _is_retryable(exc: BaseException) -> bool:
    """Retry only on transient failures: HTTP 429 / 5xx and transport errors."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 429 or status >= 500
    return isinstance(exc, (httpx.TransportError, httpx.TimeoutException))


# Exponential backoff with jitter, max 5 attempts (per spec).
_retry_policy = retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential_jitter(initial=1, max=30),
    retry=retry_if_exception(_is_retryable),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)


@dataclass(frozen=True)
class AccountInsights:
    """Account-level numbers. None means the value could not be parsed."""

    followers: int | None
    views: int | None


def _extract_metric_value(entry: dict[str, Any]) -> int | None:
    """Pull an integer out of a single metric entry, defensively.

    Supports both:
      {"name": "...", "total_value": {"value": 27}}
      {"name": "...", "values": [{"value": 5}, {"value": 9}]}   # time series
    Returns None (and logs) when no usable value is present.
    """
    total_value = entry.get("total_value")
    if isinstance(total_value, dict) and "value" in total_value:
        try:
            return int(total_value["value"])
        except (TypeError, ValueError):
            logger.warning("Non-integer total_value for metric %r", entry.get("name"))

    values = entry.get("values")
    if isinstance(values, list) and values:
        last = values[-1]  # most recent data point
        if isinstance(last, dict) and "value" in last:
            try:
                return int(last["value"])
            except (TypeError, ValueError):
                logger.warning("Non-integer values[] for metric %r", entry.get("name"))

    logger.warning("Could not extract value for metric %r", entry.get("name"))
    return None


class ThreadsClient:
    """Thin, defensive wrapper over the Threads Graph API."""

    def __init__(
        self,
        access_token: str,
        user_id: str = "me",
        *,
        base_url: str = BASE_URL,
        client: httpx.Client | None = None,
    ) -> None:
        if not access_token:
            raise ValueError("access_token is required")
        self._access_token = access_token
        self._user_id = user_id or "me"
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=REQUEST_TIMEOUT)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "ThreadsClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @_retry_policy
    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}/{path.lstrip('/')}"
        # Authenticate via access_token query param; never log the token itself.
        merged = {**params, "access_token": self._access_token}
        logger.info("GET %s params=%s", url, params)
        response = self._client.get(url, params=merged)
        response.raise_for_status()
        return response.json()

    @_retry_policy
    def _post(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}/{path.lstrip('/')}"
        merged = {**params, "access_token": self._access_token}
        # Log keys only — post text is fine, but the token must never be logged.
        logger.info("POST %s params=%s", url, sorted(params.keys()))
        response = self._client.post(url, params=merged)
        response.raise_for_status()
        return response.json()

    def get_account_insights(self) -> AccountInsights:
        """Fetch followers_count and views in a single call."""
        raw = self._get(
            f"{self._user_id}/threads_insights",
            {"metric": "views,followers_count"},
        )
        logger.info("RAW account insights: %s", raw)  # first-run shape confirmation

        followers: int | None = None
        views: int | None = None
        for entry in raw.get("data", []) or []:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if name == "followers_count":
                followers = _extract_metric_value(entry)
            elif name == "views":
                views = _extract_metric_value(entry)
        return AccountInsights(followers=followers, views=views)

    def list_recent_media(self, limit: int = 10) -> list[dict[str, Any]]:
        """Fetch recent posts as a list of {id, text, timestamp} dicts."""
        raw = self._get(
            f"{self._user_id}/threads",
            {"fields": "id,text,timestamp", "limit": limit},
        )
        logger.info("RAW recent media: %s", raw)

        media: list[dict[str, Any]] = []
        for entry in raw.get("data", []) or []:
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            media.append(
                {
                    "id": str(entry.get("id")),
                    "text": entry.get("text", "") or "",
                    "timestamp": entry.get("timestamp", "") or "",
                }
            )
        return media

    def get_media_insights(self, media_id: str) -> dict[str, int | None]:
        """Fetch views and likes for a single media item."""
        if not media_id:
            raise ValueError("media_id is required")
        raw = self._get(f"{media_id}/insights", {"metric": "views,likes"})
        logger.info("RAW media insights for %s: %s", media_id, raw)

        result: dict[str, int | None] = {"views": None, "likes": None}
        for entry in raw.get("data", []) or []:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if name in result:
                result[name] = _extract_metric_value(entry)
        return result

    # --- Phase 2: publishing (requires a threads_content_publish token) ---

    def get_container_status(self, creation_id: str) -> dict[str, str]:
        """Fetch a media container's processing status.

        GET /{creation_id}?fields=status,error_message
          -> {"status": "IN_PROGRESS|FINISHED|ERROR|EXPIRED|PUBLISHED", ...}
        Returns {"status": <str|"">, "error_message": <str|"">} defensively.
        """
        if not creation_id:
            raise ValueError("creation_id is required")
        raw = self._get(creation_id, {"fields": "status,error_message"})
        logger.info("RAW container status for %s: %s", creation_id, raw)
        return {
            "status": str(raw.get("status") or ""),
            "error_message": str(raw.get("error_message") or ""),
        }

    def create_post(self, text: str, reply_to_id: str | None = None) -> str:
        """Create a TEXT media container and return its creation id.

        POST /me/threads?media_type=TEXT&text=...  ->  {"id": "<creation_id>"}
        When `reply_to_id` is given, the post is created as a reply to that
        media id (used to build threads / 連投).
        """
        if not text:
            raise ValueError("text is required to create a post")
        params = {"media_type": "TEXT", "text": text}
        if reply_to_id:
            params["reply_to_id"] = reply_to_id
        raw = self._post(f"{self._user_id}/threads", params)
        creation_id = raw.get("id")
        if not creation_id:
            logger.error("create_post: no id in response: %s", raw)
            raise RuntimeError("Threads create_post returned no creation id")
        return str(creation_id)

    def publish_post(self, creation_id: str) -> str:
        """Publish a previously created container and return the media id.

        POST /me/threads_publish?creation_id=...  ->  {"id": "<media_id>"}
        """
        if not creation_id:
            raise ValueError("creation_id is required to publish")
        raw = self._post(
            f"{self._user_id}/threads_publish",
            {"creation_id": creation_id},
        )
        media_id = raw.get("id")
        if not media_id:
            logger.error("publish_post: no id in response: %s", raw)
            raise RuntimeError("Threads publish_post returned no media id")
        return str(media_id)
