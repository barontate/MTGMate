"""Shared HTTP plumbing: one place for retries, timeouts, rate limiting and
"did we get JSON or a login page?" checks, instead of copies in every module."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any

import requests

from mtgmate_checker.helpers import config


class SessionExpiredError(RuntimeError):
    """The saved cookies no longer log us in; run `login` again."""


class OperationCancelled(Exception):
    """The user stopped the run while a request was waiting its turn."""


class RateLimiter:
    """Enforces a minimum interval between calls.

    Sleeping only for the *remaining* time (rather than a fixed sleep after
    every request) means work done between requests counts towards the delay.
    """

    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self, should_cancel: Callable[[], bool] | None = None) -> None:
        """Block until the interval has passed.

        Sleeps in short slices so a long interval (polite mode: 60 s) can still be
        cancelled promptly; raises OperationCancelled instead of sending the request.
        """
        with self._lock:
            while True:
                remaining = self._min_interval - (time.monotonic() - self._last)
                if remaining <= 0:
                    break
                if should_cancel is not None and should_cancel():
                    raise OperationCancelled("cancelled while waiting")
                time.sleep(min(remaining, 0.25))
            self._last = time.monotonic()


def build_session() -> requests.Session:
    """A Session with the project's User-Agent.

    Deliberately has NO automatic retry adapter: urllib3 would sleep inside the
    request (honouring a Retry-After of up to six hours) where Stop can't reach it.
    Retries are done by `get_with_retry`, which can be cancelled.
    """
    session = requests.Session()
    session.headers.update({"User-Agent": config.USER_AGENT})
    return session


_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


def sleep_unless_cancelled(seconds: float, should_cancel: Callable[[], bool] | None) -> None:
    """Sleep in short slices; raise OperationCancelled as soon as Stop is requested."""
    deadline = time.monotonic() + seconds
    while True:
        if should_cancel is not None and should_cancel():
            raise OperationCancelled("cancelled while waiting to retry")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.25))


def _retry_after_seconds(resp: requests.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:  # HTTP-date form
        when = parsedate_to_datetime(value)
        return max(0.0, (when - datetime.now(when.tzinfo)).total_seconds())
    except (TypeError, ValueError):
        return None


def get_with_retry(
    session: requests.Session,
    url: str,
    *,
    should_cancel: Callable[[], bool] | None = None,
    log: Callable[[str], None] | None = None,
    **kwargs: Any,
) -> requests.Response:
    """session.get with retries on dropped connections, 429 and 5xx.

    Waits `Retry-After` (capped at config.MAX_RETRY_WAIT_SECONDS) or an exponential
    backoff between attempts, always cancellable. After the last attempt the final
    response is returned (callers use raise_for_status) or the last error is raised.
    """
    kwargs.setdefault("timeout", config.REQUEST_TIMEOUT)
    attempts = config.REQUEST_RETRIES + 1
    for attempt in range(attempts):
        last = attempt == attempts - 1
        try:
            resp = session.get(url, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as e:
            if last:
                raise
            delay, reason = config.REQUEST_BACKOFF_SECONDS * 2**attempt, type(e).__name__
        else:
            if resp.status_code not in _RETRY_STATUSES or last:
                return resp
            asked = _retry_after_seconds(resp)
            delay = asked if asked is not None else config.REQUEST_BACKOFF_SECONDS * 2**attempt
            reason = f"HTTP {resp.status_code}"
        delay = min(delay, config.MAX_RETRY_WAIT_SECONDS)
        if log is not None:
            log(
                f"  {reason}: the site asked us to slow down or dropped the request; "
                f"retrying in {delay:.0f}s (attempt {attempt + 2}/{attempts})"
            )
        sleep_unless_cancelled(delay, should_cancel)
    raise AssertionError("unreachable")  # pragma: no cover


def is_html(resp: requests.Response) -> bool:
    return resp.headers.get("content-type", "").startswith("text/html")


def redirected_to_login(resp: requests.Response) -> bool:
    return "sign_in" in resp.url
