from __future__ import annotations

import contextlib
import csv
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mtgmate_checker.helpers import config
from mtgmate_checker.helpers.auth import login_and_save_cookies
from mtgmate_checker.helpers.buylist import (
    BuylistClient,
    BuylistRow,
    CartFullError,
    SessionExpiredError,
    debug_dump,
)
from mtgmate_checker.helpers.collection import (
    SellingCard,
    apply_spare,
    load_selling_cards,
    merge_duplicates,
)
from mtgmate_checker.helpers.match import Match, match_owned_cards
from mtgmate_checker.helpers.net import OperationCancelled
from mtgmate_checker.helpers.scryfall import ScryfallClient

CSV_HEADER = (
    "card_name",
    "set_code",
    "finish",
    "credit_offered",
    "mtgmate_wants",
    "you_own",
    "uuid",
    "to_sell",  # what is actually put in the cart: own - spare, capped at wants
)


def run_login(headless: bool = False) -> bool:
    """Run the interactive login. Returns True if cookies were saved.

    Playwright's sync API refuses to start inside a running asyncio loop (which
    is where Textual calls us from), so it runs on its own thread; exceptions
    are re-raised here.
    """
    outcome: dict[str, Any] = {}

    def target() -> None:
        try:
            outcome["ok"] = login_and_save_cookies(headless=headless)
        except BaseException as e:
            outcome["error"] = e

    thread = threading.Thread(target=target, name="mtgmate-login")
    thread.start()
    thread.join()
    if "error" in outcome:
        raise outcome["error"]
    return bool(outcome["ok"])


def run_diagnose(log: Callable[[str], None] = print) -> bool:
    """Read-only health check of the pieces we can't verify offline.

    Makes two GET requests (never adds anything to the cart). Returns True if the
    login works and the cart size could be read.
    """
    client = BuylistClient.from_saved_cookies(log)
    if client is None:
        log("FAIL  No saved login. Run: mtgmate-checker login")
        return False
    if not client.is_logged_in():
        log("FAIL  Saved login has expired. Run: mtgmate-checker login")
        return False
    log("OK    Logged in.")
    count = client.initial_cart_count
    if count is None:
        log("FAIL  Couldn't tell which header number is the buylist cart.")
        log("      Numeric header widgets found (send this to the dev):")
        for pill in client.pills or []:
            log(f"      {pill}")
        if not client.pills:
            log("      (none) - header markup follows:")
            log(client.header_excerpt)
        return False
    log(f"OK    Cart currently holds {count} card(s); limit is {config.CART_LIMIT}.")
    return True


def _match_to_row(m: Match) -> list[str | int]:
    return [
        m.card_name,
        m.set_code,
        m.finish,
        f"{m.credit:.2f}",
        m.mtgmate_wants,
        m.you_own,
        m.uuid,
        m.quantity_to_sell,
    ]


@dataclass(slots=True)
class SetResult:
    """Everything one set's network work produced, handed back to the main thread."""

    rows: list[BuylistRow] = field(default_factory=list)
    matches: list[Match] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)  # replayed in order by the consumer
    error: str = ""


def _process_set(
    client: BuylistClient,
    scryfall: ScryfallClient,
    set_code: str,
    cards: list[SellingCard],
    should_cancel: Callable[[], bool],
) -> SetResult:
    """Fetch one set's buylist and match the owned cards. Runs on a pool thread.

    All network I/O (mtgmate + Scryfall) lives here so sets overlap; the shared
    rate limiters inside the clients still cap the overall request rate.
    Log lines are collected, not emitted, so output stays in set order.
    """
    result = SetResult()
    if should_cancel():
        return result
    try:
        result.rows = client.fetch_set_data(set_code)
    except SessionExpiredError:
        raise  # every later request would fail the same way
    except OperationCancelled:
        return result  # stopped while waiting; the consumer sees the cancel flag
    except Exception as e:
        result.error = str(e)
        return result
    # If stopped mid-set, the consumer sees the cancel flag; partial matches are dropped.
    with contextlib.suppress(OperationCancelled):
        result.matches = match_owned_cards(
            cards,
            result.rows,
            resolve_name=lambda c: scryfall.mtgmate_name(c.name, c.scryfall_id),
            log_update=result.logs.append,
        )
    return result


class _CartHalt(Exception):
    """The cart is full and the run can't (or the user chose not to) continue."""


class CartFiller:
    """Adds matches to the cart without ever going past mtgmate's order-size limit.

    Tracks the cart size (read from the site at start, then counted as we add) and,
    when the next card would not fit, asks `on_cart_full` to pause until the user has
    submitted the order. A card that only partly fits is split across the two orders.
    """

    def __init__(
        self,
        client: BuylistClient,
        log: Callable[[str], None],
        on_cart_full: Callable[[int], bool] | None,
        limit: int,
        start_count: int,
    ) -> None:
        self._client = client
        self._log = log
        self._on_cart_full = on_cart_full
        self._limit = limit
        self.count = start_count
        self._logged_reply = False

    def _wait_for_empty_cart(self) -> None:
        self._log(f"  Cart is full ({self.count}/{self._limit} cards).")
        if self._on_cart_full is None or not self._on_cart_full(self.count):
            raise _CartHalt(
                f"the cart is full ({self._limit} cards). Submit the order on mtgmate, "
                "then run the check again."
            )
        # Re-read the real size; if it can't be read, trust that the order was submitted.
        count = self._client.get_cart_count()
        self.count = 0 if count is None else count
        self._log(f"  Continuing. Cart now holds {self.count} card(s).")

    def add(self, m: Match) -> None:
        remaining = m.quantity_to_sell
        while remaining > 0:
            room = self._limit - self.count
            if room <= 0:
                self._wait_for_empty_cart()
                continue
            qty = min(remaining, room)
            try:
                reply = self._client.add_to_cart(m.uuid, qty)
                if not self._logged_reply and reply is not None:
                    # Show the real reply shape once, so check_add_response can be verified.
                    self._logged_reply = True
                    self._log(f"  mtgmate's reply to the first add: {str(reply)[:300]}")
            except CartFullError as e:
                self._log(f"  mtgmate says the cart is full: {e}")
                self.count = self._limit  # server knows best; pause and retry this card
                continue
            except (SessionExpiredError, OperationCancelled):
                raise
            except Exception as e:
                self._log(f"  Failed to add {m.card_name} to buylist: {e}")
                return
            self.count += qty
            remaining -= qty


def run_check(
    csv_path: Path = Path("wants.csv"),
    output: Path = Path("card_results.csv"),
    debug: bool = False,
    log_update: Callable[[str], None] = print,
    progress_update: Callable[[int], None] = print,
    speed_update: Callable[[float], None] = print,
    should_cancel: Callable[[], bool] = lambda: False,
    add_to_cart: bool = True,
    max_workers: int = config.MAX_PARALLEL_SETS,
    polite: bool = False,
    on_cart_full: Callable[[int], bool] | None = None,
    spare: int = 0,
) -> list[Match] | None:
    """Check a collection CSV against mtgmate's buylist and fill the cart.

    Returns the matches found, or None if the run could not start (no/expired
    login). `should_cancel` is polled between cards so a UI can stop the run
    cleanly instead of killing the thread mid-request. With `add_to_cart=False`
    only the results CSV is written.

    Sets are fetched and matched in parallel (`max_workers` threads, 1 = serial)
    but consumed strictly in sorted set order, so the CSV, log and cart order are
    deterministic and identical to a serial run. CSV writing and cart adds stay
    on this thread.

    `polite=True` honours mtgmate's robots.txt Crawl-delay: one mtgmate request per
    `config.POLITE_DELAY_SECONDS`, strictly one at a time (`max_workers` is ignored).
    Scryfall lookups keep their normal rate.

    `spare` copies of each card are kept back (never sold). Rows for the same printing
    are combined first, so the spare is kept once per card, and a card is never added
    to the cart more than mtgmate wants.

    The cart is never filled past `config.CART_LIMIT` cards. When it is full,
    `on_cart_full(cart_size)` is called (on this thread) and should block until the
    user has submitted the order, returning True to continue or False to stop. With no
    callback the run stops there.
    """
    start_time = time.perf_counter()

    request_delay = config.POLITE_DELAY_SECONDS if polite else config.REQUEST_DELAY_SECONDS
    if polite:
        log_update(
            f"Polite mode: one mtgmate request every {request_delay:.0f}s, one at a time. "
            "This will be slow - Stop works at any time."
        )
    try:
        client = BuylistClient.from_saved_cookies(
            log_update, request_delay=request_delay, should_cancel=should_cancel
        )
        logged_in = client is not None and client.is_logged_in()
    except OperationCancelled:
        log_update("Cancelled.")
        return None
    if client is None:
        log_update("Not logged in. Use the Log in button, or run `mtgmate-checker login`.")
        return None
    if not logged_in:
        log_update(
            "Your saved login has expired. Use the Log in button, or run `mtgmate-checker login`."
        )
        return None

    loaded = load_selling_cards(csv_path, log_update)
    combined = merge_duplicates(loaded)
    if len(combined) < len(loaded):
        log_update(f"Combined {len(loaded) - len(combined)} duplicate row(s) of the same card.")
    candidates = apply_spare(combined, spare)
    owned = [c for c in candidates if c.sellable > 0]
    if len(owned) < len(candidates):
        why = f"quantity 0 or all {spare} kept as spare" if spare > 0 else "quantity 0"
        log_update(f"Skipping {len(candidates) - len(owned)} card(s) with nothing to sell ({why}).")
    by_set = defaultdict(list)
    for card in owned:
        by_set[card.set_code].append(card)
    log_update(f"{len(owned)} card entries across {len(by_set)} sets.")

    scryfall = ScryfallClient(should_cancel=should_cancel)
    all_matches: list[Match] = []
    total_checked = 0
    cancelled = False
    halted = False
    workers = 1 if polite else max(1, min(int(max_workers), len(by_set) or 1))
    log_update(f"Checking with {workers} parallel connection(s).")
    if polite:
        minutes = len(by_set) * request_delay / 60
        log_update(
            f"Estimated time for set lookups alone: ~{minutes:.0f} min "
            f"({len(by_set)} sets); each cart add adds another {request_delay:.0f}s."
        )

    filler: CartFiller | None = None
    if add_to_cart:
        start_count = client.initial_cart_count
        if start_count is None:
            log_update("Couldn't read the cart size; counting from 0 for this run.")
        else:
            log_update(f"Cart currently holds {start_count} card(s) (limit {config.CART_LIMIT}).")
        filler = CartFiller(client, log_update, on_cart_full, config.CART_LIMIT, start_count or 0)

    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="set")
    try:
        futures: list[tuple[str, list[SellingCard], Future[SetResult]]] = [
            (
                code,
                cards,
                executor.submit(_process_set, client, scryfall, code, cards, should_cancel),
            )
            for code, cards in sorted(by_set.items())
        ]
        with open(output, "w", newline="", encoding="utf-8") as out_f:
            writer = csv.writer(out_f)
            writer.writerow(CSV_HEADER)

            for set_code, cards_in_set, future in futures:
                if should_cancel():
                    cancelled = True
                    break

                log_update(f"\nChecking set: {set_code}")
                result = future.result()  # re-raises SessionExpiredError
                for line in result.logs:
                    log_update(line)
                if result.error:
                    log_update(f"  Failed to fetch: {result.error}")
                    continue

                if debug:
                    debug_dump(result.rows)

                for m in result.matches:
                    if should_cancel():
                        cancelled = True
                        break

                    log_update(
                        f"  Found: {m.card_name} [{m.finish}] - ${m.credit:.2f} - "
                        f"MTGMate wants {m.mtgmate_wants}, you have {m.you_own}"
                        + (f", keeping {m.keep}" if m.keep else "")
                        + f" -> selling {m.quantity_to_sell}"
                    )
                    writer.writerow(_match_to_row(m))
                    out_f.flush()
                    all_matches.append(m)

                    if filler is None:
                        continue
                    if m.quantity_to_sell <= 0:
                        log_update(f"  Skipped adding {m.card_name}: quantity is 0.")
                        continue
                    try:
                        filler.add(m)
                    except SessionExpiredError:
                        raise
                    except OperationCancelled:
                        cancelled = True
                        break
                    except _CartHalt as h:
                        log_update(f"Stopped: {h}")
                        halted = True
                        break

                if cancelled or halted:
                    break

                total_checked += len(cards_in_set)
                elapsed = time.perf_counter() - start_time
                speed_update(total_checked / elapsed if elapsed > 0 else 0)
                progress_update(int(total_checked / len(owned) * 100))
    except SessionExpiredError as e:
        log_update(f"\nStopped: {e}")
    finally:
        # Drop sets that haven't started; running ones finish their current request.
        executor.shutdown(wait=True, cancel_futures=True)

    elapsed = time.perf_counter() - start_time
    rate = total_checked / elapsed if elapsed > 0 else 0
    total_credit = sum(m.credit * m.quantity_to_sell for m in all_matches)

    if cancelled:
        log_update("\nCancelled.")
    log_update(f"\nDone. {len(all_matches)} matches saved to '{output}'")
    log_update(f"Estimated total credit if selling everything matched: ${total_credit:.2f}")
    log_update(
        f"Processed {total_checked} card entries ({len(by_set)} sets) in "
        f"{elapsed:.2f}s ({rate:.1f} cards/sec)"
    )
    return all_matches
