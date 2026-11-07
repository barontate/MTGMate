"""Retries must back off politely AND stay interruptible (the cause of the hang on quit)."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, cast
from unittest import mock

import requests
from support import NL

from mtgmate_checker.helpers import check, config
from mtgmate_checker.helpers.buylist import BuylistClient
from mtgmate_checker.helpers.net import OperationCancelled, RateLimiter, get_with_retry


def resp(
    status: int = 200,
    headers: dict[str, str] | None = None,
    url: str = "https://www.mtgmate.com.au/x",
) -> requests.Response:
    r = mock.Mock(spec=requests.Response)
    r.status_code, r.headers, r.url = status, headers or {}, url
    return cast(requests.Response, r)


class FakeSession:
    """Plays back a list of outcomes (responses or exceptions); the last one repeats."""

    def __init__(self, *outcomes: requests.Response | Exception) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        self.calls += 1
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def as_session(self) -> requests.Session:
        return cast(requests.Session, self)


class GetWithRetry(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.multiple(
            config, REQUEST_RETRIES=3, REQUEST_BACKOFF_SECONDS=0.01, MAX_RETRY_WAIT_SECONDS=60.0
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_success_needs_one_call_and_no_wait(self) -> None:
        s = FakeSession(resp(200))
        t0 = time.monotonic()
        self.assertEqual(get_with_retry(s.as_session(), "u").status_code, 200)
        self.assertEqual(s.calls, 1)
        self.assertLess(time.monotonic() - t0, 0.5)

    def test_retries_429_then_succeeds_and_logs(self) -> None:
        s = FakeSession(resp(429, {"Retry-After": "0"}), resp(503), resp(200))
        logs: list[str] = []
        self.assertEqual(get_with_retry(s.as_session(), "u", log=logs.append).status_code, 200)
        self.assertEqual(s.calls, 3)
        self.assertEqual(len(logs), 2)
        self.assertIn("HTTP 429", logs[0])

    def test_gives_up_after_the_last_attempt_returning_the_response(self) -> None:
        s = FakeSession(resp(500))
        self.assertEqual(get_with_retry(s.as_session(), "u").status_code, 500)
        self.assertEqual(s.calls, 4)  # 1 + 3 retries

    def test_connection_errors_retry_then_raise(self) -> None:
        s = FakeSession(requests.ConnectionError("down"))
        with self.assertRaises(requests.ConnectionError):
            get_with_retry(s.as_session(), "u")
        self.assertEqual(s.calls, 4)

    def test_404_is_not_retried(self) -> None:
        s = FakeSession(resp(404))
        self.assertEqual(get_with_retry(s.as_session(), "u").status_code, 404)
        self.assertEqual(s.calls, 1)

    def test_huge_retry_after_is_capped_and_logged(self) -> None:
        config.MAX_RETRY_WAIT_SECONDS = 0.05
        s = FakeSession(resp(429, {"Retry-After": "21600"}), resp(200))
        logs: list[str] = []
        t0 = time.monotonic()
        get_with_retry(s.as_session(), "u", log=logs.append)
        self.assertLess(time.monotonic() - t0, 2)
        self.assertIn("retrying in 0s", logs[0])

    def test_stop_interrupts_a_long_wait_within_a_second(self) -> None:
        s = FakeSession(resp(429, {"Retry-After": "3600"}))
        stopped = threading.Event()
        timer = threading.Timer(0.3, stopped.set)
        timer.start()
        t0 = time.monotonic()
        with self.assertRaises(OperationCancelled):
            get_with_retry(s.as_session(), "u", should_cancel=stopped.is_set)
        self.assertLess(time.monotonic() - t0, 2)  # not 60s, not 3600s
        self.assertEqual(s.calls, 1)

    def test_http_date_retry_after(self) -> None:
        config.MAX_RETRY_WAIT_SECONDS = 0.05
        s = FakeSession(resp(429, {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}), resp(200))
        self.assertEqual(get_with_retry(s.as_session(), "u").status_code, 200)  # past: no wait


class StopDuringThrottling(unittest.TestCase):
    def test_run_check_returns_promptly_when_stopped_while_throttled(self) -> None:
        """The reported hang: a 429 with a long Retry-After kept the worker thread asleep."""
        session = FakeSession(resp(429, {"Retry-After": "3600"}))
        client = BuylistClient(session.as_session(), "tok", RateLimiter(0))
        stopped = threading.Event()
        threading.Timer(0.5, stopped.set).start()
        client.set_cancel_check(stopped.is_set)
        csv_text = "Name,Set code,Foil,Quantity,Scryfall ID" + NL + "A,clb,normal,1,i" + NL
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "in.csv"
            src.write_text(csv_text, encoding="utf-8")
            logs: list[str] = []
            t0 = time.monotonic()
            with mock.patch.object(BuylistClient, "from_saved_cookies", return_value=client):
                check.run_check(
                    src,
                    Path(d) / "o.csv",
                    log_update=logs.append,
                    progress_update=lambda _: None,
                    speed_update=lambda _: None,
                    should_cancel=stopped.is_set,
                )
            elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 5)
        self.assertTrue(any("Cancelled" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
