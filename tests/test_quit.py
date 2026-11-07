"""Quitting the app while a check is running must not hang the process on exit."""

from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

from support import NL
from textual.widgets import Input

from mtgmate_checker.ui import app as app_mod
from mtgmate_checker.ui import settings


def quit_during_run(fake_run_check: Callable[..., Any], quit_key: str = "ctrl+q") -> float:
    """Start a run, quit mid-way, and return how long the whole thing took."""
    started = threading.Event()

    def wrapper(**kwargs: Any) -> None:
        started.set()
        fake_run_check(**kwargs)

    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            csv = Path(d) / "c.csv"
            csv.write_text("Name,Set code,Foil" + NL + "Sol Ring,CLB,normal" + NL, encoding="utf-8")
            app = app_mod.MTGMateApp()
            with (
                mock.patch.object(app_mod, "run_check", wrapper),
                mock.patch.object(settings, "save"),
                mock.patch.object(settings, "load", return_value=dict(settings.DEFAULTS)),
            ):
                async with app.run_test(size=(100, 60)) as pilot:
                    app.query_one("#collection", Input).value = str(csv)
                    await pilot.click("#run")
                    for _ in range(100):
                        await pilot.pause(0.02)
                        if started.is_set():
                            break
                    assert started.is_set(), "the check never started"
                    await pilot.press(quit_key)
                    await pilot.pause(0.1)

    t0 = time.monotonic()
    # The whole event loop, including executor shutdown, is what hung for the user.
    asyncio.run(asyncio.wait_for(scenario(), timeout=15))
    return time.monotonic() - t0


class QuitWhileRunning(unittest.TestCase):
    def test_quit_stops_a_cooperative_check_quickly(self) -> None:
        def run_check(should_cancel: Callable[[], bool] = lambda: False, **kwargs: Any) -> None:
            while not should_cancel():  # like run_check: polls between units of work
                time.sleep(0.02)

        self.assertLess(quit_during_run(run_check), 5)


class ExitWatchdog(unittest.TestCase):
    """If a check thread is stuck, quitting forces exit instead of freezing the terminal."""

    def run_with_watchdog(self, fake_run_check: Callable[..., Any]) -> list[int]:
        exits: list[int] = []
        gate = threading.Event()

        def record_exit(code: int) -> None:
            exits.append(code)

        with (
            mock.patch.object(app_mod, "EXIT_GRACE_SECONDS", 0.3),
            mock.patch.object(os, "_exit", record_exit),
        ):
            quit_during_run(lambda **kwargs: fake_run_check(gate, **kwargs))
            time.sleep(0.6)  # let the watchdog timer fire (or not)
            gate.set()  # release any stuck fake thread so the test process can finish
        return exits

    def test_forces_exit_when_the_check_ignores_cancel(self) -> None:
        def stuck(gate: threading.Event, **kwargs: Any) -> None:
            gate.wait(5)  # ignores should_cancel, like a hung network call

        self.assertEqual(self.run_with_watchdog(stuck), [0])

    def test_does_not_force_exit_when_the_check_stops_in_time(self) -> None:
        def polite(
            gate: threading.Event,
            should_cancel: Callable[[], bool] = lambda: False,
            **kwargs: Any,
        ) -> None:
            while not should_cancel():
                time.sleep(0.02)

        self.assertEqual(self.run_with_watchdog(polite), [])


if __name__ == "__main__":
    unittest.main()
