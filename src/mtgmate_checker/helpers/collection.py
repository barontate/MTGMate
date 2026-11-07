from __future__ import annotations

import csv
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

NAME_COLUMN = "Name"
SET_CODE_COLUMN = "Set code"
FOIL_COLUMN = "Foil"
QUANTITY_COLUMN = "Quantity"
SCRYFALL_ID_COLUMN = "Scryfall ID"

REQUIRED_COLUMNS = (NAME_COLUMN, SET_CODE_COLUMN, FOIL_COLUMN)

_FOIL_MAP = {
    "normal": "Nonfoil",
    "nonfoil": "Nonfoil",
    "foil": "Foil",
    "etched": "Etched",
}


@dataclass(frozen=True, slots=True)
class SellingCard:
    name: str
    set_code: str  # lowercased
    finish: str  # "Nonfoil" / "Foil" / "Etched" -- matches mtgmate's values
    owned_quantity: int  # what you actually own
    scryfall_id: str = ""
    keep: int = 0  # how many of those to keep spare (not sell)

    @property
    def sellable(self) -> int:
        return max(0, self.owned_quantity - self.keep)


def merge_duplicates(cards: list[SellingCard]) -> list[SellingCard]:
    """Combine rows for the same printing (e.g. one per condition or language).

    Without this, two rows of the same card would each add to the cart and could
    exceed what mtgmate wants. Order of first appearance is kept.
    """
    merged: dict[tuple[str, str, str, str], SellingCard] = {}
    for c in cards:
        key = (c.name, c.set_code, c.finish, c.scryfall_id)
        prev = merged.get(key)
        merged[key] = (
            c
            if prev is None
            else replace(prev, owned_quantity=prev.owned_quantity + c.owned_quantity)
        )
    return list(merged.values())


def apply_spare(cards: list[SellingCard], spare: int) -> list[SellingCard]:
    """Keep `spare` copies of each card back from selling (never more than you own).

    Run this after merge_duplicates so the spare is kept once per card, not once
    per row.
    """
    spare = max(0, int(spare))
    return [replace(c, keep=min(spare, c.owned_quantity)) for c in cards]


def _normalize_finish(raw: str) -> str:
    return _FOIL_MAP.get(raw.strip().lower(), raw.strip().title())


def _parse_quantity(raw: str) -> int:
    try:
        return int(raw)
    except ValueError:
        return 0


def load_selling_cards(csv_path: Path, log_update: Callable[[str], None]) -> list[SellingCard]:
    """Read a ManaBox-style collection export."""
    selling = []

    log_update(f"Loading selling cards from {csv_path}")
    # utf-8-sig tolerates the BOM that Excel / some exporters prepend; without it
    # the first header would read as "﻿Name" and every row would be skipped.
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)

        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            log_update(
                f"CSV is missing expected column(s): {', '.join(missing)}. "
                f"Found columns: {reader.fieldnames}"
            )

        for row in reader:
            name = (row.get(NAME_COLUMN) or "").strip()
            set_code = (row.get(SET_CODE_COLUMN) or "").strip().lower()
            if not (name and set_code):
                continue

            selling.append(
                SellingCard(
                    name=name,
                    set_code=set_code,
                    finish=_normalize_finish(row.get(FOIL_COLUMN) or ""),
                    owned_quantity=_parse_quantity((row.get(QUANTITY_COLUMN) or "0").strip()),
                    scryfall_id=(row.get(SCRYFALL_ID_COLUMN) or "").strip(),
                )
            )
    log_update(f"Loaded {len(selling)} selling cards from {csv_path}")
    return selling


@dataclass(frozen=True, slots=True)
class CollectionSummary:
    """What we can tell the user about a chosen CSV before running anything."""

    ok: bool
    message: str
    cards: int = 0
    sets: int = 0
    copies: int = 0


def summarize_collection(path_text: str) -> CollectionSummary:
    """Validate a collection CSV path and describe it, for live UI feedback."""
    path_text = path_text.strip().strip('"')
    if not path_text:
        return CollectionSummary(False, "Enter the path to your ManaBox collection .csv")
    path = Path(path_text)
    if path.is_dir():
        return CollectionSummary(False, "That is a folder, not a .csv file")
    if not path.is_file():
        return CollectionSummary(False, "File not found")

    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            fieldnames = csv.DictReader(f).fieldnames or []
        missing = [c for c in REQUIRED_COLUMNS if c not in fieldnames]
        if missing:
            return CollectionSummary(
                False,
                f"Doesn't look like a collection export: missing column(s) "
                f"{', '.join(missing)}. Found: {', '.join(fieldnames) or 'none'}",
            )
        cards = merge_duplicates(load_selling_cards(path, lambda _m: None))
    except (OSError, UnicodeDecodeError, csv.Error) as e:
        return CollectionSummary(False, f"Couldn't read file: {e}")

    if not cards:
        return CollectionSummary(False, "No cards found in this file")
    sets = len({c.set_code for c in cards})
    copies = sum(c.owned_quantity for c in cards)
    note = "" if QUANTITY_COLUMN in fieldnames else " (no Quantity column - counting 0 each)"
    return CollectionSummary(
        True,
        f"{path.name}: {len(cards)} cards ({copies} copies) across {sets} sets{note}",
        len(cards),
        sets,
        copies,
    )
