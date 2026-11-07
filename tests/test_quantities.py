"""Spare quantity and duplicate-row handling: what ends up in the cart."""

from __future__ import annotations

import unittest

from support import FakeClient, collection_csv, row, run_it
from support import card as make_card

from mtgmate_checker.helpers.buylist import BuylistRow
from mtgmate_checker.helpers.collection import SellingCard, apply_spare, merge_duplicates
from mtgmate_checker.helpers.match import Match, match_owned_cards, merge_matches_by_uuid


def card(
    name: str = "Sol Ring",
    qty: int = 1,
    sid: str = "a",
    set_code: str = "clb",
    finish: str = "Nonfoil",
) -> SellingCard:
    return make_card(name, set_code=set_code, finish=finish, qty=qty, sid=sid)


class MergeDuplicates(unittest.TestCase):
    def test_same_printing_rows_are_summed_in_first_seen_order(self) -> None:
        cards = [card("A", 1), card("B", 2, "b"), card("A", 3), card("A", 4, sid="other")]
        merged = merge_duplicates(cards)
        self.assertEqual(
            [(c.name, c.scryfall_id, c.owned_quantity) for c in merged],
            [("A", "a", 4), ("B", "b", 2), ("A", "other", 4)],
        )

    def test_finish_and_set_keep_cards_apart(self) -> None:
        cards = [card(finish="Foil"), card(finish="Nonfoil"), card(set_code="cmm")]
        self.assertEqual(len(merge_duplicates(cards)), 3)


class Spare(unittest.TestCase):
    def test_spare_is_capped_by_what_you_own(self) -> None:
        out = apply_spare([card(qty=1), card("B", 5, "b"), card("C", 0, "c")], 2)
        self.assertEqual([(c.keep, c.sellable) for c in out], [(1, 0), (2, 3), (0, 0)])

    def test_zero_or_negative_spare_keeps_nothing(self) -> None:
        for n in (0, -3):
            self.assertEqual([c.sellable for c in apply_spare([card(qty=4)], n)], [4])

    def test_spare_applies_once_per_card_after_merging_rows(self) -> None:
        # One card on two rows (e.g. NM + LP): keeping 1 means selling 1 of the 2, not 0.
        out = apply_spare(merge_duplicates([card(qty=1), card(qty=1)]), 1)
        self.assertEqual([c.sellable for c in out], [1])


class QuantityToSell(unittest.TestCase):
    def match(self, own: int, wants: int, keep: int = 0, uuid: str = "u") -> Match:
        return Match("X", "clb", "Nonfoil", 1.0, 1.0, wants, own, uuid, keep)

    def test_spare_then_cap(self) -> None:
        self.assertEqual(self.match(own=5, wants=10, keep=1).quantity_to_sell, 4)
        self.assertEqual(self.match(own=5, wants=2, keep=1).quantity_to_sell, 2)
        self.assertEqual(self.match(own=1, wants=3, keep=1).quantity_to_sell, 0)
        self.assertEqual(self.match(own=1, wants=3, keep=9).quantity_to_sell, 0)

    def test_matches_for_one_listing_are_merged_and_capped_once(self) -> None:
        merged = merge_matches_by_uuid(
            [
                self.match(3, 4, uuid="same"),
                self.match(3, 4, uuid="same"),
                self.match(1, 4, uuid="other"),
            ]
        )
        self.assertEqual(
            [(m.uuid, m.you_own, m.quantity_to_sell) for m in merged],
            [("same", 6, 4), ("other", 1, 1)],
        )  # 6 owned, but wants only 4

    def test_two_printings_resolving_to_one_row_cannot_overfill(self) -> None:
        rows: list[BuylistRow] = [row("Sol Ring", wanted=2, uuid="only-row")]
        owned = [card(qty=2, sid="p1"), card(qty=2, sid="p2")]
        matches = match_owned_cards(owned, rows, lambda c: c.name)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].quantity_to_sell, 2)


class EndToEnd(unittest.TestCase):
    def csv_text(self, entries: list[tuple[str, int, str]]) -> str:
        return collection_csv([(name, "clb", qty, sid) for name, qty, sid in entries])

    def test_spare_reduces_cart_and_csv_shows_what_was_sold(self) -> None:
        client = FakeClient([row("Sol Ring", wanted=10, uuid="u1")])
        out = run_it(client, self.csv_text([("Sol Ring", 4, "a")]), spare=1)
        self.assertEqual(client.cart, [("u1", 3)])
        self.assertEqual(out.rows[1][5:], ["4", "u1", "3"])  # you_own=4, uuid, to_sell=3
        self.assertEqual(out.rows[0][-1], "to_sell")
        self.assertEqual(len(out.result or []), 1)

    def test_everything_kept_as_spare_is_skipped_with_no_requests(self) -> None:
        client = FakeClient([row("Sol Ring", uuid="u1")])
        fetched: list[str] = []
        original = client.fetch_set_data

        def spying_fetch(set_code: str) -> list[BuylistRow]:
            fetched.append(set_code)
            return original(set_code)

        client.fetch_set_data = spying_fetch  # type: ignore[method-assign]
        out = run_it(client, self.csv_text([("Sol Ring", 1, "a")]), spare=1)
        self.assertEqual((out.result, fetched, client.cart), ([], [], []))  # set never fetched
        self.assertEqual(len(out.rows), 1)  # header only
        self.assertTrue(any("kept as spare" in line for line in out.logs))

    def test_duplicate_rows_never_exceed_what_mtgmate_wants(self) -> None:
        client = FakeClient([row("Sol Ring", wanted=3, uuid="u1")])
        text = self.csv_text([("Sol Ring", 2, "a"), ("Sol Ring", 2, "a"), ("Sol Ring", 2, "a")])
        out = run_it(client, text)
        self.assertEqual(client.cart, [("u1", 3)])  # 6 owned, 3 wanted: one add, capped
        self.assertEqual(len(out.rows) - 1, 1)  # one CSV row, not three
        self.assertTrue(any("Combined 2 duplicate" in line for line in out.logs))

    def test_spare_kept_once_across_duplicate_rows(self) -> None:
        client = FakeClient([row("Sol Ring", wanted=10, uuid="u1")])
        text = self.csv_text([("Sol Ring", 1, "a"), ("Sol Ring", 1, "a")])
        run_it(client, text, spare=1)
        self.assertEqual(client.cart, [("u1", 1)])  # own 2, keep 1 -> sell 1

    def test_zero_spare_and_no_duplicates_behaves_as_before(self) -> None:
        client = FakeClient([row("Sol Ring", wanted=3, uuid="u1")])
        run_it(client, self.csv_text([("Sol Ring", 5, "a")]))
        self.assertEqual(client.cart, [("u1", 3)])


if __name__ == "__main__":
    unittest.main()
