from __future__ import annotations

import re
from collections.abc import Callable

from mtgmate_checker.helpers import config
from mtgmate_checker.helpers.jsontypes import JsonDict
from mtgmate_checker.helpers.net import RateLimiter, build_session, get_with_retry, is_html


def parse_scryfall_data(data: JsonDict) -> JsonDict:
    """Extract the fields needed to build an mtgmate-style card name."""
    frame_effects = data.get("frame_effects") or []
    promo_types = data.get("promo_types") or []
    return {
        "name": data.get("name"),
        "set_code": data.get("set"),
        "collector_number": data.get("collector_number"),
        "borderless": data.get("border_color") == "borderless",
        "full_art": bool(data.get("full_art")),
        "retroframe": data.get("frame") == "1993",
        "promo": bool(data.get("promo")),
        "extended_art": "extendedart" in frame_effects,
        "showcase": "showcase" in frame_effects,
        "textured": "textured" in promo_types,
        "galaxy": "galaxyfoil" in promo_types,
        "surge": "surgefoil" in promo_types,
        "halo": "halofoil" in promo_types,
        "gilded": "gilded" in promo_types,
        "phyrexian": data.get("lang") == "ph",
    }


# (flag in parsed data, suffix template). ORDER MATTERS: it is the order the
# suffixes are appended to the name, and mtgmate's names depend on it.
# "{n}" is the collector number.
_NAME_SUFFIXES: tuple[tuple[str, str], ...] = (
    # frame effects etc.
    ("borderless", " (Borderless {n})"),
    ("full_art", " (Full Art {n})"),
    ("retroframe", " (Retro Frame)"),
    ("promo", " (Promo)"),
    ("showcase", " (Showcase {n})"),
    ("extended_art", " (Extended Art)"),
    ("phyrexian", " (Phyrexian)"),
    # print effects etc.
    ("textured", " (Textured Foil)"),
    ("galaxy", " (Galaxy Foil)"),
    ("surge", " (Surge Foil)"),
    ("halo", " (Halo Foil)"),
    ("gilded", " (Gilded Foil)"),
)


def build_mtgmate_name(base_name: str, scryfall: JsonDict) -> str:
    """Append mtgmate's bracketed variant suffixes to a base card name.

    Pure function (no I/O, no mutation) so it can be unit-tested directly.
    """
    n = scryfall["collector_number"]
    name = base_name + "".join(
        template.format(n=n) for flag, template in _NAME_SUFFIXES if scryfall[flag]
    )
    # Multiple bracketed parts are merged: "(A) (B)" -> "(A - B)"-style joins.
    if name.count("(") > 1:
        name = re.sub(r"\)\s*\(", " - ", name)
    return name


class ScryfallClient:
    """Scryfall lookups with their own HTTP session, rate limit and cache.

    Uses a dedicated session (not the mtgmate one) so mtgmate cookies are never
    sent to a third party, and caches by card id so repeated lookups are free.
    """

    def __init__(self, should_cancel: Callable[[], bool] | None = None) -> None:
        self._should_cancel = should_cancel
        self._session = build_session()
        self._limiter = RateLimiter(config.REQUEST_DELAY_SECONDS)
        self._cache: dict[str, JsonDict] = {}

    def fetch(self, scryfall_id: str) -> JsonDict:
        """Fetch and parse the JSON data for one card from Scryfall."""
        cached = self._cache.get(scryfall_id)
        if cached is not None:
            return cached

        self._limiter.wait(self._should_cancel)
        resp = get_with_retry(
            self._session,
            config.SCRYFALL_CARD_URL.format(scryfall_id=scryfall_id),
            should_cancel=self._should_cancel,
        )
        resp.raise_for_status()
        if is_html(resp):
            raise RuntimeError("Got HTML instead of JSON data from Scryfall.")

        parsed = parse_scryfall_data(resp.json())
        self._cache[scryfall_id] = parsed
        return parsed

    def mtgmate_name(self, name: str, scryfall_id: str) -> str:
        """The name mtgmate would list this specific printing under."""
        return build_mtgmate_name(name, self.fetch(scryfall_id))
