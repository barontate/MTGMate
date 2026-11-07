"""Shared fakes and helpers for the offline tests (no network, no browser)."""

from __future__ import annotations

import csv
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, NamedTuple
from unittest import mock

from mtgmate_checker.helpers import check
from mtgmate_checker.helpers.buylist import BuylistClient, BuylistRow, Pill
from mtgmate_checker.helpers.collection import SellingCard
from mtgmate_checker.helpers.jsontypes import JsonDict
from mtgmate_checker.helpers.match import Match

NL = "\n"


def row(
    name: str,
    set_code: str = "clb",
    finish: str = "Nonfoil",
    wanted: int = 4,
    uuid: str = "u",
) -> BuylistRow:
    """A buylist row as mtgmate would list it (credit $2.50, cash $1.00)."""
    return BuylistRow(name, set_code, "Set", finish, 1.0, 2.5, wanted, uuid)


def card(
    name: str,
    set_code: str = "clb",
    finish: str = "Nonfoil",
    qty: int = 2,
    sid: str = "sid",
) -> SellingCard:
    """A card from the user's collection."""
    return SellingCard(name, set_code, finish, qty, sid)


def collection_csv(entries: Iterable[tuple[str, str, int, str]]) -> str:
    """A ManaBox-style export. Each entry is (name, set code, quantity, scryfall id)."""
    lines = ["Name,Set code,Foil,Quantity,Scryfall ID"]
    lines += [f"{name},{code},normal,{qty},{sid}" for name, code, qty, sid in entries]
    return NL.join(lines) + NL


class FakeClient:
    """Stands in for BuylistClient: same surface that run_check uses, no network.

    `cart` records (uuid, quantity) in the order they were added. `fail_cart`, if set,
    is raised by every add.
    """

    def __init__(self, rows: list[BuylistRow], fail_cart: Exception | None = None) -> None:
        self.rows = rows
        self.fail_cart = fail_cart
        self.cart: list[tuple[str, int]] = []
        self.initial_cart_count: int | None = None  # None = "couldn't read the cart size"
        self.header_excerpt = ""
        self.pills: list[Pill] = []

    def is_logged_in(self) -> bool:
        return True

    def fetch_set_data(self, set_code: str) -> list[BuylistRow]:
        return [r for r in self.rows if r.set_code == set_code]

    def add_to_cart(self, uuid: str, qty: int) -> JsonDict | None:
        if self.fail_cart is not None:
            raise self.fail_cart
        self.cart.append((uuid, qty))
        return None

    def get_cart_count(self) -> int | None:
        return None


class SlowClient(FakeClient):
    """fetch_set_data sleeps (like a network call) and records peak concurrency."""

    def __init__(self, rows: list[BuylistRow], delay: float = 0.05) -> None:
        super().__init__(rows)
        self.delay = delay
        self.active = 0
        self.peak = 0
        self._lock = threading.Lock()

    def fetch_set_data(self, set_code: str) -> list[BuylistRow]:
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(self.delay)
        with self._lock:
            self.active -= 1
        return super().fetch_set_data(set_code)


class RunOutput(NamedTuple):
    result: list[Match] | None
    rows: list[list[str]]  # the results CSV, header first
    logs: list[str]


def run_it(
    client: object,
    csv_text: str,
    *,
    cancel: Callable[[], bool] = lambda: False,
    **kwargs: Any,
) -> RunOutput:
    """Run `run_check` against `client` and a temporary collection file."""
    with tempfile.TemporaryDirectory() as d:
        src, out = Path(d) / "in.csv", Path(d) / "out.csv"
        src.write_text(csv_text, encoding="utf-8")
        logs: list[str] = []
        with mock.patch.object(BuylistClient, "from_saved_cookies", return_value=client):
            result = check.run_check(
                src,
                out,
                log_update=logs.append,
                progress_update=lambda _: None,
                speed_update=lambda _: None,
                should_cancel=cancel,
                **kwargs,
            )
        with open(out, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
    return RunOutput(result, rows, logs)
