from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace

from mtgmate_checker.helpers.buylist import BuylistRow
from mtgmate_checker.helpers.collection import SellingCard
from mtgmate_checker.helpers.net import OperationCancelled


@dataclass(frozen=True, slots=True)
class Match:
    card_name: str
    set_code: str
    finish: str
    credit: float
    cash: float
    mtgmate_wants: int
    you_own: int
    uuid: str
    keep: int = 0  # copies held back as spare

    @property
    def quantity_to_sell(self) -> int:
        """How many to put in the cart: what we own minus the spare, capped at what
        mtgmate wants."""
        return min(max(self.you_own - self.keep, 0), self.mtgmate_wants)


def merge_matches_by_uuid(matches: Iterable[Match]) -> list[Match]:
    """One match per mtgmate listing.

    Different printings in the collection can resolve to the same buylist row; adding
    each separately could exceed what mtgmate wants, so their quantities are summed
    and the cap is applied once.
    """
    merged: dict[str, Match] = {}
    for m in matches:
        prev = merged.get(m.uuid)
        merged[m.uuid] = (
            m
            if prev is None
            else replace(prev, you_own=prev.you_own + m.you_own, keep=prev.keep + m.keep)
        )
    return list(merged.values())


# Given an owned card, returns the exact mtgmate name for that printing.
NameResolver = Callable[[SellingCard], str]


def match_owned_cards(
    owned: Iterable[SellingCard],
    rows: Iterable[BuylistRow],
    resolve_name: NameResolver,
    log_update: Callable[[str], None] = print,
) -> list[Match]:
    """
    Matches on name + set_code + finish. A nonfoil card you own won't be
    matched against a foil-only buylist entry, even if the price would be
    tempting -- that'd be a wrong/unsellable match.

    mtgmate lists variants as "Name (Borderless 123)" etc., so a card matches
    any row whose name is `Name` or starts with `Name `. When that is ambiguous,
    `resolve_name` (a Scryfall lookup) decides which variant it is.
    """
    # Bucket rows by (set, finish) once, so each card scans only its own bucket
    # instead of the whole set (the old code rescanned every row per card).
    buckets: dict[tuple[str, str], list[BuylistRow]] = defaultdict(list)
    for r in rows:
        buckets[(r.set_code, r.finish)].append(r)

    matches = []
    for card in owned:
        prefix = card.name + " "
        candidates = [
            r
            for r in buckets.get((card.set_code, card.finish), ())
            if r.name == card.name or r.name.startswith(prefix)
        ]

        name = card.name
        row = None
        if len(candidates) == 1:
            row = candidates[0]
        elif len(candidates) > 1:
            try:
                name = resolve_name(card)
                row = next((r for r in candidates if r.name == name), None)
            except OperationCancelled:
                raise  # Stop was pressed: don't treat it as a per-card failure
            except Exception as e:  # network / bad data: skip this card, keep going
                log_update(f"Error occurred while correcting card name for {card.name}: {e}")
                name, row = card.name, None

        if row and row.wanted > 0:
            matches.append(
                Match(
                    card_name=name,
                    set_code=card.set_code,
                    finish=card.finish,
                    credit=row.credit,
                    cash=row.cash,
                    mtgmate_wants=row.wanted,
                    you_own=card.owned_quantity,
                    uuid=row.uuid,
                    keep=card.keep,
                )
            )
    return merge_matches_by_uuid(matches)
