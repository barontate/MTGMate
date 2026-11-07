"""`mtgmate-checker diagnose` and the first-reply log line."""

from __future__ import annotations

import unittest
from unittest import mock

from support import FakeClient, collection_csv, row, run_it

from mtgmate_checker.helpers import check
from mtgmate_checker.helpers.buylist import BuylistClient, Pill, header_excerpt
from mtgmate_checker.helpers.jsontypes import JsonDict


class Client(FakeClient):
    def __init__(self, count: int | None, excerpt: str = "<nav>x</nav>") -> None:
        super().__init__([])
        self.initial_cart_count = count
        self.header_excerpt = excerpt


class Diagnose(unittest.TestCase):
    def run_diag(self, client: FakeClient | None) -> tuple[bool, list[str]]:
        logs: list[str] = []
        with mock.patch.object(BuylistClient, "from_saved_cookies", return_value=client):
            ok = check.run_diagnose(logs.append)
        return ok, logs

    def test_ok_when_count_readable(self) -> None:
        ok, logs = self.run_diag(Client(12))
        self.assertTrue(ok)
        self.assertTrue(any("holds 12" in line for line in logs))

    def test_fail_lists_header_widgets_when_buylist_pill_unidentified(self) -> None:
        client = Client(None)
        pill: Pill = {"component": "CartQuantityPill", "channel": "", "href": "/cart", "value": 0}
        client.pills = [pill]
        ok, logs = self.run_diag(client)
        self.assertFalse(ok)
        self.assertTrue(any("'href': '/cart'" in line for line in logs))

    def test_fail_prints_markup_when_no_widgets_at_all(self) -> None:
        client = Client(None, "<nav>MARKUP</nav>")
        client.pills = []
        ok, logs = self.run_diag(client)
        self.assertFalse(ok)
        self.assertIn("<nav>MARKUP</nav>", logs)

    def test_no_login(self) -> None:
        ok, logs = self.run_diag(None)
        self.assertFalse(ok)
        self.assertIn("mtgmate-checker login", logs[0])

    def test_header_excerpt(self) -> None:
        self.assertIn("<nav", header_excerpt("<html><body><nav><a>hi</a></nav></body></html>"))
        self.assertIn("no <nav>", header_excerpt("<p>x</p>"))


class FirstReplyLogged(unittest.TestCase):
    def test_first_add_reply_is_logged_once(self) -> None:
        class Replying(FakeClient):
            def __init__(self) -> None:
                super().__init__([row("A", uuid="a"), row("B", uuid="b")])
                self.initial_cart_count = 0

            def add_to_cart(self, uuid: str, qty: int) -> JsonDict | None:
                super().add_to_cart(uuid, qty)
                return {"status": "ok", "total": 7}

            def get_cart_count(self) -> int | None:
                return 0

        csv_text = collection_csv([("A", "clb", 1, "i"), ("B", "clb", 1, "j")])
        out = run_it(Replying(), csv_text)
        self.assertEqual(sum("reply to the first add" in line for line in out.logs), 1)
        self.assertTrue(any("'total': 7" in line for line in out.logs))


if __name__ == "__main__":
    unittest.main()
