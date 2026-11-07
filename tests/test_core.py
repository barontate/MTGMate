"""Core pipeline tests: naming, matching, parsing, run_check, parallelism, login thread."""

from __future__ import annotations

import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from support import FakeClient, SlowClient, card, collection_csv, row, run_it

from mtgmate_checker.helpers import check
from mtgmate_checker.helpers.buylist import (
    BuylistClient,
    BuylistRow,
    SessionExpiredError,
    parse_set_data,
)
from mtgmate_checker.helpers.collection import SellingCard, load_selling_cards
from mtgmate_checker.helpers.match import match_owned_cards
from mtgmate_checker.helpers.net import OperationCancelled, RateLimiter
from mtgmate_checker.helpers.scryfall import build_mtgmate_name, parse_scryfall_data


class ScryfallNaming(unittest.TestCase):
    def base(self, **kw: Any) -> dict[str, Any]:
        data: dict[str, Any] = {"name": "X", "set": "clb", "collector_number": "345"}
        data.update(kw)
        return parse_scryfall_data(data)

    def test_plain(self) -> None:
        self.assertEqual(build_mtgmate_name("Sol Ring", self.base()), "Sol Ring")

    def test_single_suffix(self) -> None:
        s = self.base(border_color="borderless")
        self.assertEqual(build_mtgmate_name("Sol Ring", s), "Sol Ring (Borderless 345)")

    def test_multiple_suffixes_joined_in_original_order(self) -> None:
        s = self.base(border_color="borderless", promo=True, promo_types=["surgefoil"])
        self.assertEqual(
            build_mtgmate_name("Sol Ring", s), "Sol Ring (Borderless 345 - Promo - Surge Foil)"
        )

    def test_null_lists_tolerated(self) -> None:
        s = parse_scryfall_data({"frame_effects": None, "promo_types": None})
        self.assertFalse(s["showcase"] or s["galaxy"])


class Matching(unittest.TestCase):
    def test_single_candidate_matches_without_lookup(self) -> None:
        resolver = mock.Mock()
        m = match_owned_cards([card("Sol Ring")], [row("Sol Ring")], resolver)
        self.assertEqual(len(m), 1)
        resolver.assert_not_called()

    def test_finish_and_set_must_match(self) -> None:
        rows = [row("Sol Ring", finish="Foil"), row("Sol Ring", set_code="cmm")]
        self.assertEqual(match_owned_cards([card("Sol Ring")], rows, mock.Mock()), [])

    def test_prefix_requires_space_boundary(self) -> None:
        # "Sol Ring" must not match "Sol Ringleader"
        self.assertEqual(
            match_owned_cards([card("Sol Ring")], [row("Sol Ringleader")], mock.Mock()), []
        )

    def test_ambiguous_uses_resolver_and_reports_resolved_name(self) -> None:
        rows = [row("Sol Ring", uuid="a"), row("Sol Ring (Borderless 345)", uuid="b")]
        m = match_owned_cards([card("Sol Ring")], rows, lambda c: "Sol Ring (Borderless 345)")
        self.assertEqual((m[0].uuid, m[0].card_name), ("b", "Sol Ring (Borderless 345)"))

    def test_resolver_failure_is_logged_and_skipped(self) -> None:
        rows = [row("Sol Ring"), row("Sol Ring (Promo)")]
        logs: list[str] = []

        def boom(c: SellingCard) -> str:
            raise RuntimeError("net down")

        self.assertEqual(match_owned_cards([card("Sol Ring")], rows, boom, logs.append), [])
        self.assertIn("net down", logs[0])

    def test_stop_during_a_lookup_is_not_treated_as_a_card_failure(self) -> None:
        rows = [row("Sol Ring"), row("Sol Ring (Promo)")]

        def stopped(c: SellingCard) -> str:
            raise OperationCancelled("stop")

        with self.assertRaises(OperationCancelled):
            match_owned_cards([card("Sol Ring")], rows, stopped)

    def test_zero_wanted_not_matched(self) -> None:
        self.assertEqual(
            match_owned_cards([card("Sol Ring")], [row("Sol Ring", wanted=0)], mock.Mock()), []
        )

    def test_input_cards_not_mutated(self) -> None:
        c = card("Sol Ring")
        rows = [row("Sol Ring"), row("Sol Ring (Promo)")]
        match_owned_cards([c], rows, lambda _: "Sol Ring (Promo)")
        self.assertEqual(c.name, "Sol Ring")


class Parsing(unittest.TestCase):
    def test_parse_set_data_skips_malformed(self) -> None:
        good: dict[str, Any] = {
            "name": "A",
            "set_code": "clb",
            "set_name": "S",
            "finish": "Foil",
            "buy_price": 150,
            "credit_price": 200,
            "quantity": 3,
            "uuid": "u1",
        }
        rows = parse_set_data({"uuid_data": {"1": good, "2": {"name": "bad"}, "3": None}})
        self.assertEqual([(r.name, r.cash, r.credit) for r in rows], [("A", 1.5, 2.0)])

    def test_collection_handles_bom_and_bad_quantity(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.csv"
            p.write_text(
                "﻿Name,Set code,Foil,Quantity,Scryfall ID\n"
                "Sol Ring,CMM,normal,x,abc\n,CMM,foil,1,\n",
                encoding="utf-8",
            )
            cards = load_selling_cards(p, lambda _m: None)
        self.assertEqual(cards, [SellingCard("Sol Ring", "cmm", "Nonfoil", 0, "abc")])


class RunCheck(unittest.TestCase):
    CSV = collection_csv([("Sol Ring", "CLB", 5, "a"), ("Foo", "CLB", 1, "b")])

    def test_end_to_end_csv_header_matches_data_and_cart_capped(self) -> None:
        client = FakeClient([row("Sol Ring", wanted=3, uuid="u1")])
        out = run_it(client, self.CSV)
        self.assertEqual(len(out.rows[0]), len(out.rows[1]))  # header/data column count agree
        self.assertEqual(out.rows[1], ["Sol Ring", "clb", "Nonfoil", "2.50", "3", "5", "u1", "3"])
        self.assertEqual(client.cart, [("u1", 3)])  # min(own=5, wants=3)
        self.assertEqual(len(out.result or []), 1)

    def test_no_sets_does_not_crash(self) -> None:
        out = run_it(FakeClient([]), collection_csv([]))
        self.assertEqual(out.result, [])
        self.assertTrue(any("Done." in line for line in out.logs))

    def test_session_expiry_aborts_instead_of_retrying_every_card(self) -> None:
        client = FakeClient(
            [row("Sol Ring", uuid="u1"), row("Foo", uuid="u2")],
            fail_cart=SessionExpiredError("expired"),
        )
        out = run_it(client, self.CSV)
        self.assertEqual(len(out.rows) - 1, 1)  # stopped after the first failed add
        self.assertTrue(any("Stopped: expired" in line for line in out.logs))

    def test_other_cart_errors_are_logged_and_run_continues(self) -> None:
        client = FakeClient(
            [row("Sol Ring", uuid="u1"), row("Foo", uuid="u2")], fail_cart=RuntimeError("boom")
        )
        out = run_it(client, self.CSV)
        self.assertEqual(len(out.result or []), 2)
        self.assertEqual(sum("boom" in line for line in out.logs), 2)

    def test_cancel_stops_before_work(self) -> None:
        client = FakeClient([row("Sol Ring")])
        out = run_it(client, self.CSV, cancel=lambda: True)
        self.assertEqual((out.result, client.cart), ([], []))
        self.assertTrue(any("Cancelled" in line for line in out.logs))

    def test_missing_login_returns_none(self) -> None:
        with mock.patch.object(BuylistClient, "from_saved_cookies", return_value=None):
            self.assertIsNone(check.run_check(log_update=lambda _m: None))


class ParallelRun(unittest.TestCase):
    SETS = ("aaa", "bbb", "ccc", "ddd", "eee", "fff")
    CSV_TEXT = collection_csv([(f"Card {c}", c, 1, f"id{c}") for c in SETS])

    def rows(self) -> list[BuylistRow]:
        return [row(f"Card {c}", set_code=c, uuid=f"u-{c}") for c in self.SETS]

    def test_sets_overlap_but_output_order_is_deterministic(self) -> None:
        parallel = SlowClient(self.rows())
        par = run_it(parallel, self.CSV_TEXT, max_workers=4)
        serial = SlowClient(self.rows())
        ser = run_it(serial, self.CSV_TEXT, max_workers=1)
        self.assertGreater(parallel.peak, 1)  # really ran concurrently
        self.assertEqual(serial.peak, 1)
        self.assertEqual(par.rows, ser.rows)  # same CSV regardless of timing
        self.assertEqual([r[1] for r in par.rows[1:]], sorted(self.SETS))
        self.assertEqual(parallel.cart, serial.cart)

    def test_session_expiry_in_a_worker_aborts_cleanly(self) -> None:
        class Expiring(SlowClient):
            def fetch_set_data(self, set_code: str) -> list[BuylistRow]:
                if set_code == "ccc":
                    raise SessionExpiredError("expired")
                return super().fetch_set_data(set_code)

        out = run_it(Expiring(self.rows()), self.CSV_TEXT, max_workers=4)
        self.assertTrue(any("Stopped: expired" in line for line in out.logs))
        self.assertEqual([r[1] for r in out.rows[1:]], ["aaa", "bbb"])

    def test_one_failing_set_does_not_stop_others(self) -> None:
        class Flaky(SlowClient):
            def fetch_set_data(self, set_code: str) -> list[BuylistRow]:
                if set_code == "bbb":
                    raise RuntimeError("500")
                return super().fetch_set_data(set_code)

        out = run_it(Flaky(self.rows()), self.CSV_TEXT, max_workers=3)
        self.assertEqual(len(out.result or []), 5)
        self.assertTrue(any("Failed to fetch: 500" in line for line in out.logs))

    def test_cancel_stops_remaining_sets(self) -> None:
        client = SlowClient(self.rows(), delay=0.05)
        calls = {"n": 0}

        def cancel() -> bool:
            calls["n"] += 1
            return calls["n"] > 6

        out = run_it(client, self.CSV_TEXT, cancel=cancel, max_workers=2)
        self.assertLess(len(out.result or []), len(self.SETS))
        self.assertTrue(any("Cancelled" in line for line in out.logs))


class PoliteMode(unittest.TestCase):
    def run_polite(self, client: FakeClient, csv_text: str, **kw: Any) -> tuple[Any, list[str]]:
        """Run with a mocked client factory so the delay it was built with can be checked."""
        with (
            mock.patch.object(BuylistClient, "from_saved_cookies", return_value=client) as factory,
            tempfile.TemporaryDirectory() as d,
        ):
            src = Path(d) / "in.csv"
            src.write_text(csv_text, encoding="utf-8")
            logs: list[str] = []
            check.run_check(
                src,
                Path(d) / "o.csv",
                log_update=logs.append,
                progress_update=lambda _: None,
                speed_update=lambda _: None,
                **kw,
            )
        return factory, logs

    def test_polite_uses_60s_delay_and_single_connection(self) -> None:
        sets = ("aaa", "bbb", "ccc")
        client = SlowClient([row(f"Card {c}", set_code=c, uuid=f"u-{c}") for c in sets])
        csv_text = collection_csv([(f"Card {c}", c, 1, f"id{c}") for c in sets])
        factory, logs = self.run_polite(client, csv_text, polite=True, max_workers=8)
        self.assertEqual(factory.call_args.kwargs["request_delay"], 60.0)
        self.assertEqual(client.peak, 1)  # max_workers ignored
        self.assertTrue(any("Polite mode" in line for line in logs))
        self.assertTrue(any("1 parallel" in line for line in logs))

    def test_normal_mode_uses_default_delay(self) -> None:
        factory, _ = self.run_polite(FakeClient([]), collection_csv([]))
        self.assertEqual(factory.call_args.kwargs["request_delay"], 0.3)


class RateLimiterCancel(unittest.TestCase):
    def test_long_wait_is_interruptible(self) -> None:
        limiter = RateLimiter(60.0)
        limiter.wait()  # first call is free; the next would wait ~60s
        start = time.monotonic()
        with self.assertRaises(OperationCancelled):
            limiter.wait(should_cancel=lambda: True)
        self.assertLess(time.monotonic() - start, 2)

    def test_spacing_is_enforced(self) -> None:
        limiter = RateLimiter(0.2)
        limiter.wait()
        start = time.monotonic()
        limiter.wait()
        self.assertGreaterEqual(time.monotonic() - start, 0.15)


class RunLogin(unittest.TestCase):
    def test_runs_off_the_calling_thread_inside_an_event_loop(self) -> None:
        seen: dict[str, Any] = {}

        def fake_login(headless: bool = False) -> bool:
            seen["thread"] = threading.current_thread()
            try:
                asyncio.get_running_loop()
                seen["loop"] = True  # what makes Playwright's sync API refuse to start
            except RuntimeError:
                seen["loop"] = False
            return True

        async def caller() -> bool:
            with mock.patch.object(check, "login_and_save_cookies", fake_login):
                return check.run_login()

        self.assertTrue(asyncio.run(caller()))
        self.assertFalse(seen["loop"])
        self.assertIsNot(seen["thread"], threading.main_thread())

    def test_errors_propagate_to_caller(self) -> None:
        with (
            mock.patch.object(check, "login_and_save_cookies", side_effect=ImportError("pw")),
            self.assertRaises(ImportError),
        ):
            check.run_login()


if __name__ == "__main__":
    unittest.main()
