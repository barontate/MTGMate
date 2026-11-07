"""Headless tests for the Textual UI (uses Textual's Pilot; no terminal, no network)."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import Callable, Coroutine
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from unittest import mock

from support import NL
from textual.widgets import Button, Checkbox, Input, Label, RichLog

from mtgmate_checker.helpers import config
from mtgmate_checker.helpers.collection import CollectionSummary, summarize_collection
from mtgmate_checker.ui import app as app_mod
from mtgmate_checker.ui import settings

Scenario = Callable[[], Coroutine[Any, Any, None]]


class SettingsStore(unittest.TestCase):
    def test_round_trip_and_wipe(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            remembered = settings.DEFAULTS.copy()
            remembered["remember"] = True
            remembered["collection_path"] = "a.csv"
            remembered["spare"] = 2
            settings.save(remembered, p)
            loaded = settings.load(p)
            self.assertEqual((loaded["collection_path"], loaded["spare"]), ("a.csv", 2))
            settings.save(settings.DEFAULTS, p)  # remember is False: the file is wiped
            self.assertFalse(p.exists())
            self.assertEqual(settings.load(p), settings.DEFAULTS)

    def test_not_restored_unless_remember_was_ticked(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            p.write_text('{"remember": false, "collection_path": "a.csv"}', encoding="utf-8")
            self.assertEqual(settings.load(p), settings.DEFAULTS)

    def test_corrupt_file_falls_back_to_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            p.write_text("{nope", encoding="utf-8")
            self.assertEqual(settings.load(p), settings.DEFAULTS)

    def test_wrongly_typed_values_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            p.write_text(
                '{"remember": true, "spare": "lots", "polite": 1, "collection_path": "a.csv"}',
                encoding="utf-8",
            )
            loaded = settings.load(p)
            self.assertEqual(loaded["spare"], settings.DEFAULTS["spare"])  # str, not int
            self.assertEqual(loaded["polite"], settings.DEFAULTS["polite"])  # int, not bool
            self.assertEqual(loaded["collection_path"], "a.csv")  # valid ones still load


class InputHelpers(unittest.TestCase):
    def test_output_path_default_extension_and_anchor(self) -> None:
        root = config.PROJECT_ROOT
        self.assertEqual(app_mod.output_path(""), root / "mtg wants.csv")
        self.assertEqual(app_mod.output_path("  "), root / "mtg wants.csv")
        self.assertEqual(app_mod.output_path("results"), root / "results.csv")
        self.assertEqual(app_mod.output_path("Results.CSV"), root / "Results.CSV")
        abs_path = Path(tempfile.gettempdir()) / "x.csv"
        self.assertEqual(app_mod.output_path(f'"{abs_path}"'), abs_path)

    def test_parse_spare(self) -> None:
        self.assertEqual(app_mod.parse_spare("2"), 2)
        self.assertEqual(app_mod.parse_spare(""), 0)
        self.assertEqual(app_mod.parse_spare("-4"), 0)
        self.assertEqual(app_mod.parse_spare("x"), 0)

    def test_parse_parallel_clamps(self) -> None:
        self.assertEqual(app_mod.parse_parallel("3"), 3)
        self.assertEqual(app_mod.parse_parallel("0"), 1)
        self.assertEqual(app_mod.parse_parallel("99"), 8)
        self.assertEqual(app_mod.parse_parallel(""), config.MAX_PARALLEL_SETS)


class CollectionFeedback(unittest.TestCase):
    def summarize(self, text: str) -> tuple[CollectionSummary, CollectionSummary]:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.csv"
            p.write_text(text, encoding="utf-8")
            return summarize_collection(str(p)), summarize_collection(f'"{p}"')

    def test_good_manabox_file(self) -> None:
        good, quoted = self.summarize("Name,Set code,Foil,Quantity\nA,CLB,normal,2\nB,CMM,foil,1\n")
        self.assertTrue(good.ok)
        self.assertEqual((good.cards, good.sets, good.copies), (2, 2, 3))
        self.assertTrue(quoted.ok)  # Windows "Copy as path" adds quotes

    def test_wrong_file_names_missing_columns(self) -> None:
        bad, _ = self.summarize("email,total\nx,1\n")
        self.assertFalse(bad.ok)
        self.assertIn("Name", bad.message)
        self.assertIn("email", bad.message)

    def test_empty_missing_and_folder(self) -> None:
        self.assertFalse(summarize_collection("").ok)
        self.assertEqual(summarize_collection("nope.csv").message, "File not found")
        with tempfile.TemporaryDirectory() as d:
            self.assertIn("folder", summarize_collection(d).message)
        empty, _ = self.summarize("Name,Set code,Foil\n")
        self.assertFalse(empty.ok)


class AppFlow(unittest.TestCase):
    def run_app(self, scenario: Scenario) -> None:
        with (
            mock.patch.object(settings, "SETTINGS_PATH", Path(tempfile.mkdtemp()) / "s.json"),
            mock.patch.object(settings, "load", return_value=dict(settings.DEFAULTS)),
            mock.patch.object(settings, "save"),
        ):
            asyncio.run(scenario())

    def test_csv_status_updates_live_and_login_status_shown(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as d:
                csv = Path(d) / "c.csv"
                csv.write_text("Name,Set code,Foil,Quantity" + NL + "A,CLB,normal,2" + NL)
                app = app_mod.MTGMateApp()
                async with app.run_test(size=(100, 60)) as pilot:
                    status = app.query_one("#csv-status", Label)
                    self.assertTrue(status.has_class("bad"))
                    app.query_one("#collection", Input).value = str(csv)
                    await pilot.pause()
                    self.assertTrue(status.has_class("ok"))
                    self.assertTrue(app.query_one("#login-status", Label).render())

        self.run_app(scenario)

    def test_login_button_hands_terminal_over_and_refreshes(self) -> None:
        async def scenario() -> None:
            app = app_mod.MTGMateApp()
            with (
                mock.patch.object(app_mod, "run_login", return_value=True) as login,
                mock.patch.object(app_mod.MTGMateApp, "suspend", lambda self: nullcontext()),
                mock.patch("builtins.input", return_value=""),
            ):
                async with app.run_test(size=(100, 60)) as pilot:
                    await pilot.click("#login")
                    await pilot.pause()
                    login.assert_called_once()

        self.run_app(scenario)

    def test_login_falls_back_when_suspend_unsupported(self) -> None:
        async def scenario() -> None:
            app = app_mod.MTGMateApp()
            with mock.patch.object(app_mod, "run_login") as login:
                async with app.run_test(size=(100, 60)) as pilot:
                    await pilot.click("#login")  # headless driver can't suspend
                    await pilot.pause()
                    login.assert_not_called()
                    shown = str(app.query_one("#log", RichLog).lines)
                    self.assertIn("mtgmate-checker login", shown)

        self.run_app(scenario)

    def test_polite_checkbox_disables_parallel_box(self) -> None:
        async def scenario() -> None:
            app = app_mod.MTGMateApp()
            async with app.run_test(size=(100, 60)) as pilot:
                self.assertFalse(app.query_one("#parallel", Input).disabled)
                app.query_one("#polite", Checkbox).value = True
                await pilot.pause()
                self.assertTrue(app.query_one("#parallel", Input).disabled)

        self.run_app(scenario)

    def test_run_validates_inputs(self) -> None:
        async def scenario() -> None:
            app = app_mod.MTGMateApp()
            async with app.run_test(size=(100, 60)) as pilot:
                await pilot.click("#run")
                await pilot.pause()
                self.assertTrue(app.query_one("#log", RichLog).lines)

        self.run_app(scenario)

    def test_run_calls_run_check_and_reenables_buttons(self) -> None:
        calls: dict[str, Any] = {}

        def fake_run_check(**kwargs: Any) -> None:
            calls.update(kwargs)
            kwargs["log_update"]("hello")
            kwargs["progress_update"](50)
            kwargs["speed_update"](1.5)

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as d:
                csv = Path(d) / "c.csv"
                csv.write_text("Name,Set code,Foil" + NL + "Sol Ring,CLB,normal" + NL)
                app = app_mod.MTGMateApp()
                with mock.patch.object(app_mod, "run_check", fake_run_check):
                    async with app.run_test(size=(100, 60)) as pilot:
                        app.query_one("#collection", Input).value = str(csv)
                        app.query_one("#output", Input).value = ""
                        app.query_one("#spare", Input).value = "2"
                        await pilot.click("#run")
                        await app.workers.wait_for_complete()
                        await pilot.pause()
                        self.assertFalse(app.query_one("#run", Button).disabled)
                        self.assertTrue(app.query_one("#stop", Button).disabled)
            self.assertEqual(calls["csv_path"], csv)
            self.assertTrue(calls["add_to_cart"])
            self.assertEqual(calls["output"].name, "mtg wants.csv")  # default used when blank
            self.assertEqual(calls["max_workers"], config.MAX_PARALLEL_SETS)
            self.assertFalse(calls["polite"])
            self.assertEqual(calls["spare"], 2)  # the "keep spare" box reaches run_check

        self.run_app(scenario)


if __name__ == "__main__":
    unittest.main()
