import os
import sys
from pathlib import Path

BASE_URL = "https://www.mtgmate.com.au"
LOGIN_URL = BASE_URL + "/users/sign_in"
SCRYFALL_CARD_URL = "https://api.scryfall.com/cards/{scryfall_id}"

# The buylist page for a set is client-side rendered (React); the actual
# card data comes from this JSON endpoint underneath it, discovered via
# browser DevTools -> Network -> XHR.
SET_DATA_PATH = "/buylist/magic_sets/{slug}/data"
ADD_ITEM_PATH = "/buylist/cart/add_item_json"
# Page assumed to show the `cart-card-total` element (unverified; diagnose will tell).
CART_PAGE_PATH = "/buylist/cart"
# Any page that renders the CSRF meta tag for a logged-in user.
CSRF_SOURCE_PATH = "/cards/buylist_search?q=Forest"

# A page that redirects to /users/sign_in when logged out. (/buylist does NOT
# -- it's public, so it can't tell whether a session is valid.)
LOGIN_CHECK_PATH = "/users/edit"

# Where runtime data (login cookies, saved settings, results CSVs) lives. From a checkout this
# is the project folder; in the packaged .exe it is the folder containing the .exe, so the
# data stays next to the program no matter which directory it is launched from.
if getattr(sys, "frozen", False):
    PROJECT_ROOT = Path(sys.executable).resolve().parent
else:
    PROJECT_ROOT = Path(__file__).resolve().parents[3]  # src/mtgmate_checker/helpers/config.py
_HERE = PROJECT_ROOT  # backwards-compatible alias
# Gitignored: holds a live session.
COOKIE_JAR_PATH = PROJECT_ROOT / "mtgmate_cookies.json"

# Where Playwright keeps its Firefox. In the packaged .exe Playwright's default is inside
# PyInstaller's temp folder (_MEI...), which is deleted on exit, so use a permanent one.
# None = leave Playwright's default (running from a checkout).
if getattr(sys, "frozen", False):
    BROWSERS_PATH: Path | None = (
        Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "mtgmate-checker" / "browsers"
    )
else:
    BROWSERS_PATH = None

REQUEST_TIMEOUT = 15
# How many sets are fetched/matched at once. The shared rate limiters below still
# cap the *overall* request rate, so this overlaps waiting time without raising it.
MAX_PARALLEL_SETS = 4
# Minimum gap between two requests to the same host (politeness / rate limit).
REQUEST_DELAY_SECONDS = 0.3
# Retries for transient failures (429 / 5xx / dropped connections).
REQUEST_RETRIES = 3
REQUEST_BACKOFF_SECONDS = 1.0
# Longest we'll wait on a server's Retry-After before retrying (always cancellable).
MAX_RETRY_WAIT_SECONDS = 60.0
USER_AGENT = "Mozilla/5.0 (compatible; mtgmate-buylist-checker/1.0; personal want-list tool)"

# "Polite mode": honours the Crawl-delay: 60 in mtgmate's robots.txt (checked 2026-11-07).
# One request per minute, strictly one at a time. Very slow by design.
POLITE_DELAY_SECONDS = 60.0

# mtgmate's maximum buylist order size. Confirmed: counts total quantity (cards), not lines.
# MTGMATE_CART_LIMIT lowers it for testing the pause flow with a small cart.
CART_LIMIT = int(os.environ.get("MTGMATE_CART_LIMIT", "300"))
