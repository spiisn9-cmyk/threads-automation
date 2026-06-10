"""Tests for ThreadsClient response parsing (httpx is mocked; no real API)."""
from __future__ import annotations

import httpx

from src.clients.threads_client import ThreadsClient


def _client_with(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_account_insights_total_value():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "threads_insights" in request.url.path
        assert request.url.params["metric"] == "views,followers_count"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"name": "followers_count", "total_value": {"value": 27}},
                    {"name": "views", "total_value": {"value": 1234}},
                ]
            },
        )

    tc = ThreadsClient("token", "me", client=_client_with(handler))
    insights = tc.get_account_insights()
    assert insights.followers == 27
    assert insights.views == 1234


def test_account_insights_views_as_time_series():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"name": "followers_count", "total_value": {"value": 10}},
                    {"name": "views", "values": [{"value": 5}, {"value": 9}]},
                ]
            },
        )

    tc = ThreadsClient("token", "me", client=_client_with(handler))
    insights = tc.get_account_insights()
    assert insights.followers == 10
    assert insights.views == 9  # most recent data point


def test_account_insights_missing_value_is_defensive():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"name": "followers_count"}]})

    tc = ThreadsClient("token", "me", client=_client_with(handler))
    insights = tc.get_account_insights()
    assert insights.followers is None
    assert insights.views is None


def test_account_insights_empty_data():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    tc = ThreadsClient("token", "me", client=_client_with(handler))
    insights = tc.get_account_insights()
    assert insights.followers is None
    assert insights.views is None


def test_list_recent_media_parses_and_skips_idless_entries():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["fields"] == "id,text,timestamp"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "1", "text": "hello", "timestamp": "2026-06-10T00:00:00+0000"},
                    {"id": "2"},  # missing text/timestamp -> defaulted to ""
                    {"text": "no id"},  # skipped
                ]
            },
        )

    tc = ThreadsClient("token", "me", client=_client_with(handler))
    media = tc.list_recent_media(limit=5)
    assert [m["id"] for m in media] == ["1", "2"]
    assert media[0]["text"] == "hello"
    assert media[1]["text"] == ""
    assert media[1]["timestamp"] == ""


def test_media_insights_parses_views_and_likes():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/abc/insights" in request.url.path
        return httpx.Response(
            200,
            json={
                "data": [
                    {"name": "views", "total_value": {"value": 100}},
                    {"name": "likes", "total_value": {"value": 7}},
                ]
            },
        )

    tc = ThreadsClient("token", "me", client=_client_with(handler))
    stats = tc.get_media_insights("abc")
    assert stats["views"] == 100
    assert stats["likes"] == 7


def test_create_post_returns_creation_id():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path.endswith("/me/threads")
        assert request.url.params["media_type"] == "TEXT"
        assert request.url.params["text"] == "hello"
        return httpx.Response(200, json={"id": "creation-123"})

    tc = ThreadsClient("token", "me", client=_client_with(handler))
    assert tc.create_post("hello") == "creation-123"


def test_publish_post_returns_media_id():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path.endswith("/me/threads_publish")
        assert request.url.params["creation_id"] == "creation-123"
        return httpx.Response(200, json={"id": "media-999"})

    tc = ThreadsClient("token", "me", client=_client_with(handler))
    assert tc.publish_post("creation-123") == "media-999"


def test_create_post_missing_id_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    tc = ThreadsClient("token", "me", client=_client_with(handler))
    try:
        tc.create_post("hello")
    except RuntimeError:
        return
    raise AssertionError("expected RuntimeError when response has no id")


def test_retry_on_500_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, json={"error": "server"})
        return httpx.Response(
            200,
            json={
                "data": [
                    {"name": "followers_count", "total_value": {"value": 3}},
                    {"name": "views", "total_value": {"value": 4}},
                ]
            },
        )

    tc = ThreadsClient("token", "me", client=_client_with(handler))
    insights = tc.get_account_insights()
    assert insights.followers == 3
    assert calls["n"] == 2  # one failure + one success
