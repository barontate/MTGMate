"""BuylistClient.get_cart_count: which pages it asks, and what it does when the count is missing."""

from __future__ import annotations

import unittest
from typing import Any, cast
from unittest import mock

import requests

from mtgmate_checker.helpers import config
from mtgmate_checker.helpers.buylist import BuylistClient, SessionExpiredError
from mtgmate_checker.helpers.net import RateLimiter


def reply(html: str, url: str = "https://www.mtgmate.com.au/x") -> requests.Response:
    r = mock.Mock(spec=requests.Response)
    r.text, r.url, r.status_code = html, url, 200
    r.raise_for_status = lambda: None
    return cast(requests.Response, r)


def client_with(pages: dict[str, requests.Response]) -> tuple[BuylistClient, mock.Mock]:
    session = mock.Mock(spec=requests.Session)

    def get(url: str, **kwargs: Any) -> requests.Response:
        return pages[url.replace(config.BASE_URL, "")]

    session.get.side_effect = get
    return BuylistClient(session, "tok", RateLimiter(0)), session


class CartCountPages(unittest.TestCase):
    def test_cart_page_is_tried_first(self) -> None:
        client, session = client_with(
            {
                config.CART_PAGE_PATH: reply('<b id="cart-card-total">31</b>'),
                config.CSRF_SOURCE_PATH: reply("<html></html>"),
            }
        )
        self.assertEqual(client.get_cart_count(), 31)
        self.assertEqual(session.get.call_count, 1)  # stops at the first page that answers

    def test_falls_back_to_search_page(self) -> None:
        client, session = client_with(
            {
                config.CART_PAGE_PATH: reply("<html>nothing</html>"),
                config.CSRF_SOURCE_PATH: reply('<b id="cart-card-total">4</b>'),
            }
        )
        self.assertEqual(client.get_cart_count(), 4)
        self.assertEqual(session.get.call_count, 2)

    def test_none_when_neither_page_has_it(self) -> None:
        client, _ = client_with(
            {
                config.CART_PAGE_PATH: reply("<html></html>"),
                config.CSRF_SOURCE_PATH: reply("<html></html>"),
            }
        )
        self.assertIsNone(client.get_cart_count())

    def test_zero_is_returned_not_treated_as_missing(self) -> None:
        client, session = client_with(
            {
                config.CART_PAGE_PATH: reply('<b id="cart-card-total">0</b>'),
                config.CSRF_SOURCE_PATH: reply("<html></html>"),
            }
        )
        self.assertEqual(client.get_cart_count(), 0)
        self.assertEqual(session.get.call_count, 1)

    def test_logged_out_redirect_raises(self) -> None:
        client, _ = client_with(
            {
                config.CART_PAGE_PATH: reply("", url="https://www.mtgmate.com.au/users/sign_in"),
                config.CSRF_SOURCE_PATH: reply(""),
            }
        )
        with self.assertRaises(SessionExpiredError):
            client.get_cart_count()


if __name__ == "__main__":
    unittest.main()
