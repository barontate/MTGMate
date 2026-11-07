"""Cart-limit behaviour: never exceed mtgmate's 300-card order, pause, split, resume."""

from __future__ import annotations

import unittest
from collections.abc import Callable

from support import FakeClient, collection_csv, row, run_it

from mtgmate_checker.helpers.buylist import (
    BuylistRow,
    CartFullError,
    CartRejectedError,
    check_add_response,
    parse_cart_count,
)
from mtgmate_checker.helpers.jsontypes import JsonDict


class CartResponses(unittest.TestCase):
    def test_success_shapes_pass(self) -> None:
        replies: list[object] = [{"status": "success"}, {"success": True}, {"items": 3}, [], "text"]
        for reply in replies:
            check_add_response(reply)

    def test_full_vs_other_rejection(self) -> None:
        with self.assertRaises(CartFullError):
            check_add_response({"status": "error", "message": "Maximum of 300 cards per order"})
        with self.assertRaises(CartFullError):
            check_add_response({"success": False, "error": "Your cart is full"})
        with self.assertRaises(CartRejectedError) as cm:
            check_add_response({"errors": ["Card not found"]})
        self.assertNotIsInstance(cm.exception, CartFullError)


class CartCountParsing(unittest.TestCase):
    # Real markup from the live site (shop cart pill), plus a buylist pill of the same shape.
    SHOP_PILL = (
        '<li class="nav-item"><div data-react-cache-id="CartQuantityPill-0" '
        'data-react-class="CartQuantityPill" data-react-props=\'{"channelId":"cartQuantity",'
        '"initialValue":0,"hydrateUrl":null}\'></div>'
        '<a class="nav-link" href="/cart">Cart </a></li>'
    )

    def page(self, *items: str) -> str:
        return f"<html><body><nav><div><ul>{''.join(items)}</ul></div></nav></body></html>"

    def buylist_pill(
        self, value: int, channel: str = "buylistCartQuantity", href: str = "/buylist/cart"
    ) -> str:
        return (
            '<li class="nav-item"><div data-react-class="CartQuantityPill" '
            f'data-react-props=\'{{"channelId":"{channel}","initialValue":{value}}}\'></div>'
            f'<a class="nav-link" href="{href}">Buylist</a></li>'
        )

    def test_cart_card_total_element_by_id_or_class(self) -> None:
        by_id = '<html><body><nav></nav><span id="cart-card-total">57</span></body></html>'
        by_class = '<html><body><div class="x cart-card-total"> 120 cards </div></body></html>'
        self.assertEqual(parse_cart_count(by_id), 57)
        self.assertEqual(parse_cart_count(by_class), 120)

    def test_cart_card_total_handles_zero_commas_and_prefers_it_over_pills(self) -> None:
        zero = '<p id="cart-card-total">0</p>'
        self.assertEqual(parse_cart_count(zero), 0)  # 0 is a real answer, not "missing"
        self.assertEqual(parse_cart_count('<b id="cart-card-total">Total: 1,234</b>'), 1234)
        page = self.page(self.SHOP_PILL, self.buylist_pill(5)) + '<i id="cart-card-total">9</i>'
        self.assertEqual(parse_cart_count(page), 9)

    def test_cart_card_total_empty_or_react_placeholder(self) -> None:
        react = (
            '<div id="cart-card-total" data-react-class="Total" '
            "data-react-props='{\"initialValue\": 11}'></div>"
        )
        self.assertEqual(parse_cart_count(react), 11)
        self.assertIsNone(parse_cart_count('<span id="cart-card-total"></span>'))

    def test_shop_cart_pill_alone_is_still_ignored(self) -> None:
        self.assertIsNone(parse_cart_count(self.page(self.SHOP_PILL)))

    def test_reads_buylist_pill_not_shop_cart(self) -> None:
        # The shop cart says 0; the buylist cart says 42. Must return 42.
        self.assertEqual(parse_cart_count(self.page(self.SHOP_PILL, self.buylist_pill(42))), 42)

    def test_identifies_buylist_pill_by_either_channel_or_link(self) -> None:
        by_href = self.buylist_pill(7, channel="qty2", href="/buylist/cart")
        by_channel = self.buylist_pill(9, channel="buylistQuantity", href="/somewhere")
        self.assertEqual(parse_cart_count(self.page(self.SHOP_PILL, by_href)), 7)
        self.assertEqual(parse_cart_count(self.page(self.SHOP_PILL, by_channel)), 9)

    def test_ambiguous_or_missing_returns_none(self) -> None:
        two = self.page(self.buylist_pill(1), self.buylist_pill(2))
        self.assertIsNone(parse_cart_count(two))
        self.assertIsNone(parse_cart_count("<html><body><nav></nav></body></html>"))
        self.assertIsNone(parse_cart_count("not html at all"))

    def test_bad_props_are_skipped(self) -> None:
        bad = '<li><div data-react-class="X" data-react-props="{nope"></div></li>'
        self.assertEqual(parse_cart_count(self.page(bad, self.buylist_pill(3))), 3)


class CartClient(FakeClient):
    """Tracks the cart like mtgmate: optionally rejects past the limit; submit empties it."""

    def __init__(
        self,
        rows: list[BuylistRow],
        start: int = 0,
        known_start: bool = True,
        limit: int = 300,
        server_enforces: bool = False,
    ) -> None:
        super().__init__(rows)
        self.initial_cart_count = start if known_start else None
        self.server_cart = start
        self.limit = limit
        self.server_enforces = server_enforces
        self.orders: list[list[tuple[str, int]]] = [[]]  # one list of (uuid, qty) per order

    def add_to_cart(self, uuid: str, qty: int) -> JsonDict | None:
        if self.server_enforces and self.server_cart + qty > self.limit:
            raise CartFullError("Maximum of 300 cards per order")
        self.server_cart += qty
        self.orders[-1].append((uuid, qty))
        return None

    def get_cart_count(self) -> int | None:
        return self.server_cart

    def submit_order(self) -> None:
        self.server_cart = 0
        self.orders.append([])


class CartCap(unittest.TestCase):
    def csv_text(self, qtys: list[int]) -> str:
        return collection_csv([(f"Card{i}", "clb", q, f"id{i}") for i, q in enumerate(qtys)])

    def rows(self, qtys: list[int]) -> list[BuylistRow]:
        return [row(f"Card{i}", wanted=q, uuid=f"u{i}") for i, q in enumerate(qtys)]

    def submitting(self, client: CartClient, prompts: list[int]) -> Callable[[int], bool]:
        """An `on_cart_full` that records the prompt, 'submits' the order, and continues."""

        def on_full(count: int) -> bool:
            prompts.append(count)
            client.submit_order()
            return True

        return on_full

    def test_never_exceeds_limit_and_splits_a_card_across_orders(self) -> None:
        qtys = [250, 100, 100]
        client = CartClient(self.rows(qtys))
        prompts: list[int] = []
        out = run_it(client, self.csv_text(qtys), on_cart_full=self.submitting(client, prompts))
        self.assertEqual(len(out.result or []), 3)
        self.assertEqual(prompts, [300])
        for order in client.orders:
            self.assertLessEqual(sum(q for _, q in order), 300)
        self.assertEqual(sum(q for o in client.orders for _, q in o), 450)  # nothing lost
        self.assertEqual(client.orders[0], [("u0", 250), ("u1", 50)])
        self.assertEqual(client.orders[1], [("u1", 50), ("u2", 100)])

    def test_existing_cart_contents_are_counted(self) -> None:
        qtys = [50]
        client = CartClient(self.rows(qtys), start=290)
        run_it(client, self.csv_text(qtys), on_cart_full=self.submitting(client, []))
        self.assertEqual(client.orders[0], [("u0", 10)])
        self.assertEqual(client.orders[1], [("u0", 40)])

    def test_no_callback_stops_the_run_at_the_limit(self) -> None:
        qtys = [300, 10, 10]
        client = CartClient(self.rows(qtys))
        out = run_it(client, self.csv_text(qtys), on_cart_full=None)
        self.assertEqual(client.server_cart, 300)
        self.assertEqual([r[0] for r in out.rows[1:]], ["Card0", "Card1"])
        self.assertTrue(any("Stopped: the cart is full" in line for line in out.logs))

    def test_user_choosing_stop_halts(self) -> None:
        qtys = [300, 10]
        client = CartClient(self.rows(qtys))
        out = run_it(client, self.csv_text(qtys), on_cart_full=lambda _count: False)
        self.assertEqual(client.server_cart, 300)
        self.assertTrue(any("Stopped: the cart is full" in line for line in out.logs))

    def test_server_saying_full_triggers_pause_even_if_count_unknown(self) -> None:
        qtys = [200, 200]
        client = CartClient(self.rows(qtys), start=150, known_start=False, server_enforces=True)
        prompts: list[int] = []
        run_it(client, self.csv_text(qtys), on_cart_full=self.submitting(client, prompts))
        self.assertGreaterEqual(len(prompts), 1)  # the server's "full" is believed
        self.assertEqual(sum(q for o in client.orders for _, q in o), 400)

    def test_dry_run_never_touches_cart_or_prompts(self) -> None:
        qtys = [300, 300]
        client = CartClient(self.rows(qtys))
        prompted: list[int] = []

        def on_full(count: int) -> bool:
            prompted.append(count)
            return True

        run_it(client, self.csv_text(qtys), add_to_cart=False, on_cart_full=on_full)
        self.assertEqual((client.server_cart, prompted), (0, []))


if __name__ == "__main__":
    unittest.main()
