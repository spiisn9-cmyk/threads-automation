"""Tests for SheetsClient's transient-error retry + reconnect.

Reproduces the publish-job failure: after a long jitter sleep the reused
httplib2 connection is dead, so the next Sheets call raises a transport error.
The client must retry, rebuild the connection, and succeed.
"""
from __future__ import annotations

import http.client

import pytest

from src.clients.sheets_client import (
    _execute_with_retry,
    _is_transient_sheets_error,
)


class _Req:
    """Fake request whose .execute() yields the next queued outcome."""

    def __init__(self, outcomes: list) -> None:
        self._outcomes = outcomes

    def execute(self):
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_transport_errors_are_transient():
    assert _is_transient_sheets_error(ConnectionResetError("reset"))
    assert _is_transient_sheets_error(BrokenPipeError("pipe"))
    assert _is_transient_sheets_error(TimeoutError("t"))
    assert _is_transient_sheets_error(http.client.RemoteDisconnected("bye"))
    # not transient
    assert not _is_transient_sheets_error(ValueError("bad range"))
    assert not _is_transient_sheets_error(KeyError("nope"))


def test_retry_reconnects_then_succeeds():
    # 1st execute: stale connection error (post-sleep). 2nd: success.
    outcomes = [ConnectionResetError("stale socket"), {"values": [["ok"]]}]
    req = _Req(outcomes)
    rebuilds: list[int] = []
    slept: list[float] = []

    result = _execute_with_retry(
        lambda: req,
        rebuild_fn=lambda: rebuilds.append(1),
        op="read 'post_queue'",
        sleep_fn=lambda s: slept.append(s),
    )
    assert result == {"values": [["ok"]]}
    assert rebuilds == [1], "connection must be rebuilt before the retry"
    assert slept, "should back off before retrying"


def test_non_transient_raises_immediately_without_rebuild():
    req = _Req([ValueError("invalid range")])
    rebuilds: list[int] = []
    with pytest.raises(ValueError):
        _execute_with_retry(
            lambda: req,
            rebuild_fn=lambda: rebuilds.append(1),
            op="read",
            sleep_fn=lambda s: None,
        )
    assert rebuilds == [], "non-transient errors must not trigger a rebuild/retry"


def test_exhausts_attempts_and_raises_underlying():
    err = ConnectionResetError("always stale")
    req = _Req([err, err, err, err])  # all attempts fail
    with pytest.raises(ConnectionResetError):
        _execute_with_retry(
            lambda: req,
            rebuild_fn=lambda: None,
            op="read",
            max_attempts=4,
            sleep_fn=lambda s: None,
        )
