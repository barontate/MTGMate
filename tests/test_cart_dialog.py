"""The cart-full dialog: blocks the worker thread until the user answers."""

from __future__ import annotations

import asyncio
import threading
import unittest

from textual.pilot import Pilot

from mtgmate_checker.ui import app as app_mod


async def _open_dialog(
    app: app_mod.MTGMateApp, pilot: Pilot[None]
) -> tuple[threading.Thread, dict[str, bool]]:
    """Ask the question from a worker thread, as run_check does, and wait for the dialog."""
    out: dict[str, bool] = {}

    def ask() -> None:
        out["v"] = app._ask_cart_full(300)

    thread = threading.Thread(target=ask)
    thread.start()
    for _ in range(60):
        await pilot.pause(0.05)
        if isinstance(app.screen, app_mod.CartFullScreen):
            break
    return thread, out


async def _wait_done(pilot: Pilot[None], thread: threading.Thread) -> None:
    for _ in range(60):
        await pilot.pause(0.05)
        if not thread.is_alive():
            break
    thread.join(1)


class CartDialog(unittest.TestCase):
    def check_button(self, button: str, expected: bool) -> None:
        async def scenario() -> None:
            app = app_mod.MTGMateApp()
            async with app.run_test(size=(100, 60)) as pilot:
                thread, out = await _open_dialog(app, pilot)
                self.assertIsInstance(app.screen, app_mod.CartFullScreen)
                self.assertTrue(thread.is_alive())  # worker is blocked, waiting for the user
                await pilot.click(button)
                await _wait_done(pilot, thread)
                self.assertEqual(out.get("v"), expected)

        asyncio.run(scenario())

    def test_continue_returns_true(self) -> None:
        self.check_button("#cart-continue", True)

    def test_stop_here_returns_false(self) -> None:
        self.check_button("#cart-stop", False)

    def test_main_stop_closes_dialog_and_releases_worker(self) -> None:
        async def scenario() -> None:
            app = app_mod.MTGMateApp()
            async with app.run_test(size=(100, 60)) as pilot:
                thread, out = await _open_dialog(app, pilot)
                app._cancel.set()  # the main Stop was pressed
                await _wait_done(pilot, thread)
                self.assertEqual(out.get("v"), False)
                self.assertNotIsInstance(app.screen, app_mod.CartFullScreen)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
