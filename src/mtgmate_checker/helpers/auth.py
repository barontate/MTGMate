from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

from mtgmate_checker.helpers import config

if TYPE_CHECKING:  # Playwright is imported lazily at runtime; only needed for the type
    from playwright.sync_api import BrowserContext


def _context_is_logged_in(context: BrowserContext) -> bool:
    """
    Load a login-only page in a fresh tab of this context. Tabs share cookies,
    so this reflects the login whichever tab it happened in. (A real page load
    is needed: Cloudflare 403s Playwright's `context.request` client.)
    """
    probe = context.new_page()
    try:
        resp = probe.goto(config.BASE_URL + config.LOGIN_CHECK_PATH, wait_until="domcontentloaded")
        # Logged out -> redirected to /users/sign_in. Anything but a clean
        # load (e.g. a 403 challenge) is treated as "can't confirm".
        return resp is not None and resp.ok and "sign_in" not in probe.url
    finally:
        probe.close()


def saved_login_time() -> datetime | None:
    """When the cookie jar was last written, or None if there is no saved login."""
    try:
        return datetime.fromtimestamp(config.COOKIE_JAR_PATH.stat().st_mtime)
    except OSError:
        return None


def _install_firefox() -> bool:
    """Download Playwright's Firefox using the driver bundled with the playwright package.

    Works from source and from the packaged .exe (where there is no `playwright` command).
    """
    import subprocess

    from playwright._impl._driver import compute_driver_executable, get_driver_env

    print("Firefox for the login window isn't installed yet. Downloading it (one-off)...")
    node, cli = compute_driver_executable()
    result = subprocess.run([node, cli, "install", "firefox"], env=get_driver_env(), check=False)
    return result.returncode == 0


def login_and_save_cookies(headless: bool = False) -> bool:
    """Interactive login; returns True if cookies were saved."""
    # Imported lazily: Playwright is only needed for this one-off step, so the
    # GUI/check path doesn't require it to be installed.
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            browser = p.firefox.launch(headless=headless)
        except Exception as exc:
            if "Executable doesn't exist" not in str(exc) or not _install_firefox():
                raise
            browser = p.firefox.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()
        page.goto(config.LOGIN_URL)

        # Credentials are typed into the browser window by the user, so this tool
        # never sees or stores a password. We don't guess mtgmate's form selectors
        # either; the script just waits for the user to confirm they're signed in.
        print("A browser window has opened on the mtgmate sign-in page.")
        print("Enter your email and password there, solve the CAPTCHA (if shown) and")
        input("click Sign In. Then press Enter here once you're logged in...")

        # Confirm we're actually logged in before saving -- if the login
        # failed silently we'd otherwise save a useless cookie jar.
        #
        # Don't judge this from a tab's URL: the login can finish in a
        # different tab/window than `page`, and `context.pages[-1]` isn't
        # reliably that one, so we'd read a stale sign-in page. Instead ask the
        # server directly with the context's cookies, which every tab shares.
        if not _context_is_logged_in(context):
            open_tabs = ", ".join(p.url for p in context.pages) or "none"
            print(
                "mtgmate says this browser session is not logged in -- "
                "Cookies were NOT saved. Make sure you clicked Sign In and "
                f"try again. (Open tabs: {open_tabs})"
            )
            browser.close()
            return False

        cookies = context.cookies()
        browser.close()

    with open(config.COOKIE_JAR_PATH, "w", encoding="utf-8") as f:
        json.dump(cookies, f, indent=2)

    print(
        f"Logged in. Cookies saved to {config.COOKIE_JAR_PATH} -- "
        f"the app will reuse these until they expire, no browser needed."
    )
    return True
