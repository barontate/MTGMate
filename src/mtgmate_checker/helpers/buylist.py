from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypedDict

import requests
from bs4 import BeautifulSoup
from bs4.element import Tag

from mtgmate_checker.helpers import config
from mtgmate_checker.helpers.jsontypes import JsonDict
from mtgmate_checker.helpers.net import (
    RateLimiter,
    SessionExpiredError,
    build_session,
    get_with_retry,
    is_html,
    redirected_to_login,
)

__all__ = [
    "BuylistClient",
    "BuylistRow",
    "CartFullError",
    "CartRejectedError",
    "SessionExpiredError",
    "parse_set_data",
]

LogFn = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class BuylistRow:
    name: str
    set_code: str
    set_name: str
    finish: str  # "Nonfoil", "Foil", or "Etched"
    cash: float  # dollars
    credit: float  # dollars
    wanted: int  # quantity mtgmate wants to buy
    uuid: str = ""  # mtgmate uuid for the card


def parse_set_data(data: JsonDict) -> list[BuylistRow]:
    """Turn the endpoint's JSON into rows. Entries with an unexpected shape are skipped."""
    rows = []
    for e in data.get("uuid_data", {}).values():
        try:
            rows.append(
                BuylistRow(
                    name=e["name"],
                    set_code=e["set_code"],
                    set_name=e["set_name"],
                    finish=e["finish"],
                    cash=e["buy_price"] / 100,
                    credit=e["credit_price"] / 100,
                    wanted=e["quantity"],
                    uuid=e["uuid"],
                )
            )
        except (KeyError, TypeError):
            continue  # skip a malformed entry rather than crash the whole run
    return rows


class CartRejectedError(RuntimeError):
    """mtgmate answered the add-to-cart call with an error (not an HTTP failure)."""


class CartFullError(CartRejectedError):
    """The cart is at mtgmate's maximum order size."""


_FAILURE_STATUSES = {"error", "fail", "failed", "failure"}
_CART_FULL_HINTS = re.compile(r"300|maximum|\bmax\b|limit|\bfull\b|too many", re.IGNORECASE)


class Pill(TypedDict):
    """A numeric header widget (see react_pills)."""

    component: str
    channel: str
    href: str
    value: int


def _attr(tag: Tag, name: str) -> str:
    """An HTML attribute as a plain string ('' if absent; multi-valued ones are joined)."""
    value = tag.get(name)
    if value is None:
        return ""
    return value if isinstance(value, str) else " ".join(value)


def check_add_response(data: object) -> None:
    """Raise if an add_item_json reply says the add did not happen.

    The reply's exact shape isn't documented, so this is deliberately tolerant:
    it only reacts to an explicit failure marker, and tells "cart full" apart from
    other rejections by the wording of the message.
    """
    if not isinstance(data, dict):
        return
    status = str(data.get("status", "")).lower()
    failed = (
        data.get("success") is False
        or status in _FAILURE_STATUSES
        or bool(data.get("error"))
        or bool(data.get("errors"))
    )
    if not failed:
        return
    text = json.dumps(data)[:300]
    if _CART_FULL_HINTS.search(text):
        raise CartFullError(text)
    raise CartRejectedError(text)


def react_pills(html: str) -> list[Pill]:
    """Every server-rendered React widget in the page header that carries a number.

    The site renders badges like the cart count client-side: the HTML only holds an
    empty placeholder, `<div data-react-class="CartQuantityPill" data-react-props='{...
    "initialValue": 3}'>`, so the number has to be read from those props.
    """
    nav = BeautifulSoup(html, "html.parser").select_one("body > nav")
    pills: list[Pill] = []
    for div in nav.select("div[data-react-class]") if nav is not None else []:
        try:
            props = json.loads(_attr(div, "data-react-props") or "{}")
        except ValueError:
            continue
        value = props.get("initialValue") if isinstance(props, dict) else None
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        link = div.find_next_sibling("a") or (div.parent.find("a") if div.parent else None)
        pills.append(
            {
                "component": _attr(div, "data-react-class"),
                "channel": str(props.get("channelId", "")),
                "href": _attr(link, "href") if link else "",
                "value": value,
            }
        )
    return pills


CART_TOTAL_ID = "cart-card-total"  # element that shows the number of cards in the cart


def _cart_total_element_count(soup: BeautifulSoup) -> int | None:
    """Number inside the `cart-card-total` element (matched as id or class)."""
    el = soup.find(id=CART_TOTAL_ID) or soup.find(class_=CART_TOTAL_ID)
    if el is None:
        return None
    match = re.search(r"\d[\d,]*", el.get_text(" ", strip=True))
    if match:
        return int(match.group().replace(",", ""))
    # Rendered client-side? Then the number may sit in the widget's props instead.
    try:
        value = json.loads(_attr(el, "data-react-props") or "{}").get("initialValue")
    except (ValueError, AttributeError):
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def parse_cart_count(html: str) -> int | None:
    """Cards in the *buylist* cart.

    Primary source: the `cart-card-total` element. Fallback: a header widget that is
    identifiably the buylist cart. The header's shop-cart pill (link "/cart") is never
    used, since reading it would give a wrong count. Returns None when the count can't
    be established (run `mtgmate-checker diagnose`).
    """
    try:
        count = _cart_total_element_count(BeautifulSoup(html, "html.parser"))
        if count is not None:
            return count
        pills = react_pills(html)
    except (AttributeError, TypeError):
        return None
    buylist = [p for p in pills if "buylist" in (p["channel"] + p["href"]).lower()]
    if len(buylist) == 1:
        return buylist[0]["value"]
    return None


def header_excerpt(html: str, limit: int = 2500) -> str:
    """The page header's markup (no cookies/tokens live there), for diagnostics."""
    nav = BeautifulSoup(html, "html.parser").select_one("body > nav")
    return str(nav)[:limit] if nav is not None else "(no <nav> element found on the page)"


def debug_dump(rows: list[BuylistRow], limit: int = 15) -> None:
    print(f"Parsed {len(rows)} card entries. First {min(limit, len(rows))}:")
    for r in rows[:limit]:
        print(
            f"  {r.name} [{r.finish}] ({r.set_code}) -- "
            f"cash ${r.cash:.2f} / credit ${r.credit:.2f} / wanted {r.wanted}"
        )


class BuylistClient:
    """Authenticated mtgmate session: owns the cookies, CSRF token and rate limit.

    Replaces the old module-level functions that passed a bare
    `requests.Session` around with a monkey-patched `auth_token` attribute.
    """

    def __init__(
        self,
        session: requests.Session,
        csrf_token: str,
        limiter: RateLimiter | None = None,
    ) -> None:
        self._session = session
        self._csrf_token = csrf_token
        self._limiter = limiter or RateLimiter(config.REQUEST_DELAY_SECONDS)
        self._should_cancel: Callable[[], bool] | None = None
        self._log: LogFn | None = None
        self.initial_cart_count: int | None = None  # None = couldn't be read
        self.header_excerpt: str = ""  # raw <nav> markup, for diagnosing the cart badge
        self.pills: list[Pill] = []  # numeric header widgets seen (see react_pills)

    def set_cancel_check(self, should_cancel: Callable[[], bool]) -> None:
        """Lets a long rate-limit wait (polite mode) be interrupted by Stop."""
        self._should_cancel = should_cancel

    def _wait_turn(self) -> None:
        self._limiter.wait(self._should_cancel)

    def _get(self, url: str, **kwargs: Any) -> requests.Response:
        """GET with cancellable retries (Stop is honoured even while backing off)."""
        return get_with_retry(
            self._session, url, should_cancel=self._should_cancel, log=self._log, **kwargs
        )

    # -- construction -----------------------------------------------------

    @classmethod
    def from_saved_cookies(
        cls,
        log: LogFn,
        request_delay: float = config.REQUEST_DELAY_SECONDS,
        should_cancel: Callable[[], bool] | None = None,
    ) -> BuylistClient | None:
        """Build a client from the cookie jar written by `login`.

        `request_delay` is the minimum gap between mtgmate requests (every request,
        including the ones made here, goes through the same limiter).
        Returns None when no cookie jar exists. Raises RuntimeError if the CSRF
        token cannot be found.
        """
        log("Finding login session.")
        if not config.COOKIE_JAR_PATH.exists():
            return None

        log("Creating session using saved cookies.")
        with open(config.COOKIE_JAR_PATH, encoding="utf-8") as f:
            cookies = json.load(f)

        session = build_session()
        for c in cookies:
            session.cookies.set(
                c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/")
            )
        log("Session created.")

        log("Creating dynamic authentication token.")
        limiter = RateLimiter(request_delay)
        limiter.wait(should_cancel)
        resp = get_with_retry(
            session,
            config.BASE_URL + config.CSRF_SOURCE_PATH,
            should_cancel=should_cancel,
            log=log,
        )
        resp.raise_for_status()
        if redirected_to_login(resp):
            log("Something went wrong when creating authentication token.")

        client = cls(session, cls._extract_csrf_token(resp.text), limiter)
        client._should_cancel = should_cancel
        client._log = log
        # This page already carries the header, so the starting cart size is free.
        client.initial_cart_count = parse_cart_count(resp.text)
        client.header_excerpt = header_excerpt(resp.text)
        client.pills = react_pills(resp.text)
        if client.initial_cart_count is None:
            # Not on the search page: ask the cart page (one extra request).
            # On failure the caller reports "couldn't read the cart size".
            with contextlib.suppress(requests.RequestException, SessionExpiredError):
                client.initial_cart_count = client.get_cart_count()
        return client

    @staticmethod
    def _extract_csrf_token(html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        meta = soup.find("meta", {"name": "csrf-token"})
        if meta and _attr(meta, "content"):
            return _attr(meta, "content")
        field = soup.find("input", {"name": "authenticity_token"})
        if field and _attr(field, "value"):
            return _attr(field, "value")
        raise RuntimeError("Could not find authenticity_token during session load.")

    # -- API --------------------------------------------------------------

    def is_logged_in(self) -> bool:
        self._wait_turn()
        resp = self._get(config.BASE_URL + config.LOGIN_CHECK_PATH, allow_redirects=True)
        return not redirected_to_login(resp)

    def fetch_set_data(self, set_code: str) -> list[BuylistRow]:
        """Fetch and parse the JSON buylist data for one set code (e.g. 'clb')."""
        slug = set_code.lower().strip()
        url = config.BASE_URL + config.SET_DATA_PATH.format(slug=slug)

        self._wait_turn()
        resp = self._get(url)
        resp.raise_for_status()
        if redirected_to_login(resp) or is_html(resp):
            raise SessionExpiredError(
                "Got redirected to login instead of JSON data -- "
                "saved cookies have likely expired. Run `login` again."
            )
        return parse_set_data(resp.json())

    def add_to_cart(self, uuid: str, quantity: int) -> JsonDict:
        """Add `quantity` of a card to the buylist cart."""
        self._wait_turn()
        resp = self._session.post(
            config.BASE_URL + config.ADD_ITEM_PATH,
            data={
                "card_sku[id]": uuid,
                "card_sku[quantity]": str(quantity),
                "authenticity_token": self._csrf_token,
            },
            timeout=config.REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        if redirected_to_login(resp) or is_html(resp):
            raise SessionExpiredError(
                "Session expired while trying to add item to buylist. Run `login` again."
            )
        try:
            data = resp.json()
        except ValueError:
            return {"status": "success", "text": resp.text}
        check_add_response(data)
        return data if isinstance(data, dict) else {"status": "success", "data": data}

    def get_cart_count(self) -> int | None:
        """Cards currently in the buylist cart (the `cart-card-total` element).

        Tries the cart page first, then the search page. Returns None if neither
        shows it (page layout changed).
        """
        for path in (config.CART_PAGE_PATH, config.CSRF_SOURCE_PATH):
            self._wait_turn()
            resp = self._get(config.BASE_URL + path)
            resp.raise_for_status()
            if redirected_to_login(resp):
                raise SessionExpiredError("Session expired while reading the cart size.")
            count = parse_cart_count(resp.text)
            if count is not None:
                return count
        return None
