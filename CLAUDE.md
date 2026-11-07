# CLAUDE.md — MTGMate-checker

Terminal (Textual) tool that checks a ManaBox-style collection CSV against mtgmate.com.au's
buylist, writes matches to a results CSV, and adds them to the buylist cart.

## Environment

- Python 3.12+, managed with **uv**. The virtualenv is `.venv/` (direnv activates it automatically).
- Add dependencies with `uv add <pkg>` (or `uv add --dev <pkg>`). Never use `pip install` directly.
- Project created from `gh:barontate/python-template` (answers in `.copier-answers.yml`).
- Layout: `src/mtgmate_checker/` (`cli.py` entry point, `helpers/` the pipeline, `ui/` the Textual
  app); tests in `tests/` (`support.py` holds the shared fakes). Imports are absolute:
  `from mtgmate_checker.helpers import check`.
- Runtime data (cookies, saved settings, results CSVs) lives in the project root, found via
  `helpers/config.py: PROJECT_ROOT` (`parents[3]` of that file, which assumes an editable
  `uv sync` install from this checkout).
- Playwright is only needed for the one-off login (`helpers/auth.py`); run
  `uv run playwright install firefox` once.

## Commands

| Task | Command |
|---|---|
| Install / sync deps | `uv sync` |
| Run the app (Textual TUI: ctrl+r run, ctrl+x stop, ctrl+l log in, ctrl+q quit) | `uv run mtgmate-checker` (or `uv run python -m mtgmate_checker`) |
| Log in without the UI | `uv run mtgmate-checker login` |
| Read-only live check (login + cart size) | `uv run mtgmate-checker diagnose` |
| Run tests (offline; UI via Textual Pilot) | `uv run pytest` |
| Lint + autofix | `uv run ruff check --fix` |
| Format | `uv run ruff format` |
| Type check | `uv run mypy` |
| All hooks | `uv run pre-commit run --all-files` |

## Conventions (from the template)

- Code must pass ruff and mypy (strict) — run the hooks before declaring work done.
  (True as of 2026-11-07: ruff, ruff format, mypy strict and all pre-commit hooks pass.)
- Write tests for new behaviour.
- Commit messages follow Conventional Commits (`feat:`, `fix:`, `docs:`, ...).
- Never commit `mtgmate_cookies.json` (live login), `mtgmate_settings.json` or CSV data; all
  are git-ignored.

## Architecture

```
cli.py ─► ui/app.py (Textual; settings in ui/settings.py → mtgmate_settings.json)
              └─► @work(thread=True) worker, UI updates via call_from_thread
                     └─► helpers/check.py: run_check()   ← orchestrator
                           ├─ collection.py  CSV → SellingCard
                           ├─ buylist.py     BuylistClient: cookies → CSRF → set JSON → cart POST
                           ├─ match.py       match on (set, finish, name / name-prefix)
                           ├─ scryfall.py    ScryfallClient + build_mtgmate_name (variant suffixes)
                           ├─ net.py         session/retry, RateLimiter, SessionExpiredError
                           ├─ auth.py        Playwright login → mtgmate_cookies.json
                           └─ config.py      URLs, paths, timeouts
```

Per run: load cookies → fetch CSRF token → parse CSV → group by set → per set: fetch JSON,
match, write CSV row, POST to cart.

## Conventions and gotchas

- Matching: a card matches rows named `Name` or starting with `Name ` in the same set and
  finish. If more than one candidate, Scryfall (via `scryfall_id`) decides the variant name.
- Suffix order in `scryfall._NAME_SUFFIXES` is significant: mtgmate names depend on it.
- `run_check` takes callbacks (`log_update`, `progress_update`, `speed_update`,
  `should_cancel`); the UI relays them with `call_from_thread`. Never touch widgets from the worker thread.
- Stop is cooperative (`threading.Event` → `should_cancel`); never kill the thread.
- Settings save on every input change (widgets are gone by `on_unmount`).
- Keep all HTTP going through `net.build_session()` / `RateLimiter`. Scryfall uses its own
  session so mtgmate cookies are never sent to it.
- `SessionExpiredError` aborts the run; other cart/fetch errors are logged and skipped.
- Secrets (never commit/share): `mtgmate_cookies.json` (a live session). No credentials are stored: login happens in the browser.
  `tate-code/mtgmate_cookies.json` contains a live session.
- mtgmate's terms discourage bot traffic; keep the polite request rate.

## Change log

### 2026-11-07 — Architecture/quality refactor (behavior-preserving, plus bug fixes)

Bug fixes
- `scryfall.py` used `re.sub` without importing `re` (NameError on multi-variant cards).
- Result CSV header lacked `uuid` (6 header columns vs 7 data columns); header added.
- `elapsed`/`rate` undefined when no sets were processed (NameError at summary).
- Bare `except:` on cart add replaced with logged errors; `SessionExpiredError` now aborts
  instead of retrying every remaining card/set.
- Stop no longer uses `QThread.terminate()`; replaced by cooperative cancellation.
- Speed metric now uses cards checked so far, not total collection size.
- Cards with quantity 0 are still written to the CSV but not posted to the cart.
- CSV loader tolerates a UTF-8 BOM.
- `requirements.txt`: removed unused `selenium`/`numpy`; added `beautifulsoup4`, `playwright`.

Performance/scalability
- Matching buckets buylist rows by (set, finish) instead of rescanning every row per card.
- `RateLimiter` sleeps only the remaining gap instead of a fixed sleep after every request.
- Scryfall lookups cached per `scryfall_id`; separate session from mtgmate's.
- UI log capped at 2000 lines; Run button disabled while a run is active.

Maintainability
- New `helpers/net.py` (session, retry, rate limit, login-redirect checks).
- `BuylistClient` class replaces module functions + monkey-patched `session.auth_token`.
- `ScryfallClient` + pure `build_mtgmate_name` with a data-driven suffix table.
- `match_owned_cards` is pure (injected name resolver, no input mutation); `Match` gained
  `quantity_to_sell`.
- Dataclasses frozen/slotted; type hints modernized; config constants de-duplicated
  (`PROJECT_ROOT`, `UI_FILE`; `_HERE` kept as alias).
- Worker no longer receives UI widgets; exceptions in the thread are logged.
- Playwright imported lazily (only needed for login).
- UI `.ui` path anchored to the project; settings persistence table-driven.
- Added `tests/test_core.py` (19 offline tests).

Originals were backed up before the refactor (session scratchpad; no git repo).

### 2026-11-07 — Textual UI replaces PyQt6 (PO decision)
- New `ui/app.py` (Textual) and `ui/settings.py` (JSON "Remember" store, gitignored).
- Removed PyQt6, `workers/checker_worker.py`, `ui/main_window.py`, `ui/mainwindow.ui`.
- No file-browse dialog in a terminal: collection path is typed/pasted. Inputs are validated
  before a run (file must exist, output name required).
- "Add to Buylist as cards are checked" is now a real option (`run_check(add_to_cart=)`,
  default on = previous behavior; off = write the results CSV only). Resolves the old
  open question about `addAlongsideBox`.
- Dead 300-cap hooks (`show_full_message`, `resume`) went with the Qt worker; item 2 below
  must be rebuilt in Textual (e.g. a modal screen + `threading.Event`).
- Added `tests/test_ui.py` (settings store + headless app flow); 23 tests total.
- Login remains a separate console step (not in the TUI) — superseded below.

### 2026-11-07 — PO feedback: login clarity + CSV feedback
- Login was undiscoverable (messages said "run `login`" but no such command existed).
  Now: `python main.py login`, a **Log in** button / `ctrl+l` (uses `App.suspend()` to hand
  the terminal to the Playwright flow; falls back to the CLI hint if unsupported), a login
  status line (saved date or "Not logged in"), and clearer run-time messages.
  `login_and_save_cookies` now returns a bool; `saved_login_time()` reads the cookie mtime
  (it shows *a* saved login exists; validity is only confirmed when a check runs).
- Collection CSV gets live feedback under the path box (`collection.summarize_collection`):
  not found / folder / missing required columns (lists what it found) / no cards / or
  "file: N cards (M copies) across S sets". Run is blocked unless it is OK. Quoted paths
  ("Copy as path") are accepted.
- Tests: 29 total (UI tests use a 100x60 headless terminal; `suspend` is stubbed).

## Future improvements

Product owner decisions (2026-11-07) — approved, in priority order
1. [x] Apply "spare quantity" (done 2026-11-07).
2. [x] 300-card cart cap (done and verified live 2026-11-07).
3. [x] Combine duplicate CSV rows (done 2026-11-07).
4. [x] In-app login (Log in button, credentials typed in the browser). Remaining: startup session check, below.


Robustness / security
- [ ] Login: verify the saved session is still valid on startup (cheap request) instead of file date.
- [ ] Remove/relocate `tate-code/` (live cookies); add the project to git with a correct
      `.gitignore` (it currently ignores `pyproject.toml` and `wants.csv`).
- [ ] Surface "no saved login / expired login" as a dialog, not only a log line (the top status line and log message exist).
- [x] Validate paths in the UI (done: live CSV feedback, blank output uses default).

Performance
- [x] Fetch sets concurrently (done; see change log).
- [ ] Cache set JSON on disk with a short TTL for repeated runs.
- [ ] Batch/resume support: persist progress so a crashed run can continue.

Maintainability
- [ ] Add `helpers/__init__.py` and a proper entry point / packaging.
- [ ] Optional: a file picker for the collection path (Textual `DirectoryTree` modal).
- [ ] Add integration tests for `BuylistClient` using recorded responses (`responses`/`vcr`).
- [x] Update README (rewritten 2026-11-07 for the Textual app).
- [ ] CI running ruff + unit tests.

### 2026-11-07 — Login fixes (PO feedback)
- Fixed "Playwright Sync API inside the asyncio loop": `run_login` now runs the login on its
  own thread (works from both the Textual app and `python main.py login`).
- Email/password are no longer prompted in the console or read from a file/env var: the user
  types them in the browser. Removed `get_credentials`, the credentials file and its config.
  (Resolves the plaintext-credentials backlog item.)

### 2026-11-07 — Parallel checking, output default, Remember clarity (PO requests)
- `run_check` fetches + matches sets in a `ThreadPoolExecutor` (`max_workers`, default
  `config.MAX_PARALLEL_SETS` = 4, UI "Parallel connections" 1-8). Results are consumed in
  sorted set order, so CSV/log/cart order is identical to a serial run; CSV writing and
  cart adds stay on the main run thread. Shared `RateLimiter`s still space request starts
  >= `REQUEST_DELAY_SECONDS` apart, so parallelism does NOT raise the peak request rate.
- Output name defaults to `mtg wants.csv` (blank = default; `.csv` appended if missing).
- "Remember" relabelled: "Remember these settings for next time" (saves paths, spare qty,
  parallel, cart option to `mtgmate_settings.json`; unticking deletes the file).
- robots.txt (checked 2026-11-07): mtgmate has a `User-agent: *` group with
  `Disallow: /cards/search` and `Crawl-delay: 60`; Scryfall has no crawl-delay. We don't hit
  /cards/search (CSRF uses /cards/buylist_search). Crawl-delay is aimed at crawlers; PO to
  decide whether it applies to this logged-in personal tool.

### 2026-11-07 — Polite mode (PO request)
- New "Polite mode" checkbox (saved with Remember; disables the parallel box). Runs with
  `run_check(polite=True)`: every mtgmate request (startup, login check, set fetches, cart
  adds) goes through one limiter at `config.POLITE_DELAY_SECONDS` = 60s, one connection.
  Scryfall keeps its normal rate. Logs an estimated duration up front.
- `RateLimiter.wait(should_cancel)` sleeps in 0.25s slices and raises `OperationCancelled`,
  so Stop works instantly even mid-wait. `BuylistClient.from_saved_cookies` takes
  `request_delay` / `should_cancel`.
- Decision to revisit: polite delay also applies to cart adds (strict reading of the
  robots.txt Crawl-delay). Applying it to set lookups only would be a one-line change.
- 58 tests.

### 2026-11-07 — Cart limit (bug report: run kept adding past 300 cards)
- Cause: the Qt-to-Textual rewrite dropped the old Selenium "read the cart badge, stop at 300"
  check, and every add that returned HTTP 200 was assumed to have worked (a rejected add
  looked like a success).
- `CartFiller` (helpers/check.py) now owns adding: tracks cart size (starting value read
  from the site header badge on login via `parse_cart_count`; same path the old XPath used),
  never adds past `config.CART_LIMIT` (300), splits a card that only partly fits across two
  orders, and when full calls `on_cart_full(count)`. The Textual `CartFullScreen` modal
  ("Submit this order... Continue / Stop here") blocks the worker thread until answered;
  main Stop also closes it. After Continue the real count is re-read. With no callback
  (CLI) the run stops with an instruction to submit and re-run.
- `check_add_response` treats explicit error replies as failures (`CartRejectedError`) and
  "full/maximum/limit/300" wording as `CartFullError`, which triggers the pause even if our
  local count was wrong. Failed adds are now logged with the server's message.
- CONFIRMED by PO: the 300 limit counts total quantity (not distinct lines). STILL TO VERIFY on the live site:
  the header badge path still exists (else log says "Couldn't read the cart size;
  counting from 0", so a non-empty cart at start would not be accounted for); the real
  add_item_json error wording (adjust `_CART_FULL_HINTS` if the pause doesn't trigger).
- Tests: 76 (tests/test_cart.py, tests/test_cart_dialog.py).

### 2026-11-07 — Live-site verification aids
- `python main.py diagnose`: read-only (2 GETs, never touches the cart). Reports login OK and
  the cart size read from the header; if the badge can't be found it prints the header
  markup so the parser can be fixed.
- The first add of each run logs mtgmate's raw reply ("mtgmate's reply to the first add: ...")
  so `check_add_response` can be checked against the real shape.
- `MTGMATE_CART_LIMIT=<n>` env var lowers the cart limit to exercise the pause/continue flow
  with a small cart. (Our logic only; it can't show the server's real "full" wording.)
- 87 tests.

### 2026-11-07 — Cart badge parsing fixed from live diagnose output
- Finding: the header badge is client-rendered. Raw HTML only has an empty
  `<div data-react-class="CartQuantityPill" data-react-props='{"channelId":...,"initialValue":N}'>`;
  the old Selenium XPath (`...li[6]/div[1]/span`) only worked on the hydrated DOM.
- `parse_cart_count` now reads `initialValue` from those props, and ONLY from a pill that is
  identifiably the buylist one (channelId or link contains "buylist"). The shop cart pill
  (`/cart`, channel `cartQuantity`) is deliberately never used. Otherwise returns None.
- `python main.py diagnose` on failure now lists every numeric header widget
  (component/channel/href/value) so the right one can be identified.
- STILL OPEN: the first diagnose output was truncated before the buylist pill (if any), so
  whether the buylist pill is in this page's header and how it is labelled is unconfirmed.
- 92 tests.

### 2026-11-07 — Cart count from `cart-card-total` (PO found the element)
- `parse_cart_count` now reads the number in the element with id or class `cart-card-total`
  (digits, commas tolerated; 0 is a valid answer; falls back to the element's
  `data-react-props.initialValue`). The buylist-pill logic remains as a fallback; the shop
  cart pill is never used.
- `BuylistClient.get_cart_count` asks `config.CART_PAGE_PATH` (`/buylist/cart`, UNVERIFIED
  guess) first, then the search page. At login, if the search page lacks the element, one
  extra request reads the cart page.
- STILL OPEN: confirm which page URL shows `cart-card-total` (adjust `CART_PAGE_PATH`).
- 101 tests.
- VERIFIED 2026-11-07 on the live site: `diagnose` read "Cart currently holds 19 card(s)".
  Cart-size reading works. Still unverified live: the server's "cart full" reply wording
  and the pause/continue dialog end to end.
- VERIFIED 2026-11-07 by PO with `MTGMATE_CART_LIMIT` set low: the pause dialog, Continue and
  resume work end to end. Only the server's real "cart full" wording (reached via an actual
  300) remains unobserved; the local count prevents reaching it in normal use.

### 2026-11-07 — Spare quantity + duplicate rows (backlog items 1 and 3)
- Pipeline in `run_check`: load -> `merge_duplicates` (same name/set/finish/scryfall_id are
  summed) -> `apply_spare` (keep N per card, once per card, never more than owned) -> drop
  cards with nothing to sell -> group by set. Sets with nothing to sell are never fetched.
- After matching, `merge_matches_by_uuid` sums different printings that resolve to the same
  mtgmate listing, so one add per listing: cart quantity = min(owned - spare, mtgmate wants).
- Results CSV gains a final `to_sell` column (what actually went into the cart); `you_own`
  is still the true owned count. Log lines show "keeping N -> selling M".
- Behavior changes: cards with quantity 0 are no longer processed or written to the CSV;
  duplicate rows become a single CSV row.
- UI: the existing spare box is now wired (`parse_spare`, `run_check(spare=)`).
- 121 tests.

### 2026-11-07 — Hang on quit (KeyboardInterrupt in asyncio shutdown)
- Symptom: after quitting, the terminal froze until Ctrl+C (traceback ended in
  `asyncio ... shutdown_default_executor`). Cause (inferred; only the tail of the traceback was
  seen): Textual runs the check on asyncio's default thread pool, and Python waits for those
  threads at exit. A worker could be asleep inside urllib3's built-in retry, which honours
  `Retry-After` (default cap: 6 HOURS) and can't be cancelled.
- Fix 1: removed the urllib3 `Retry` adapter. `net.get_with_retry` does retries on 429/5xx/
  dropped connections itself: honours `Retry-After` but capped at `MAX_RETRY_WAIT_SECONDS`
  (60), logs "retrying in Ns", and sleeps in 0.25s slices so Stop / quit interrupts it
  (`OperationCancelled`). Used by mtgmate and Scryfall clients; Scryfall lookups and the
  rate limiter are cancellable too.
- Fix 2 (safety net): quitting with a check still running starts an 8s watchdog
  (`EXIT_GRACE_SECONDS`); if the check thread is still alive it prints a message and
  `os._exit(0)`. Safe because results are flushed per match and settings save on change.
- Gotcha for tests: anything that leaves a non-cancellable thread alive will make
  `asyncio.run` hang at close; keep workers polling `should_cancel`.
- 139 tests (tests/test_retry.py, tests/test_quit.py).


### 2026-11-07 — Moved into the python-template project (MTGMate-checker)
- All code, tests, data files and `.vscode/` were copied over identically from `MTGMate/`.
- Merged by hand: `pyproject.toml` (runtime deps textual/requests/beautifulsoup4/playwright;
  dev dep `types-requests`; `uv.lock` regenerated), `.gitignore` (the template's ignored
  nothing of ours: cookies, settings and CSVs would have been committed), README.md and this
  file. The template's `.pre-commit-config.yaml` was kept as is.
- Not carried over on purpose: the old project's `claude-commit-msg` pre-commit hook (the
  template was created with `use_claude_commits: false`) and old `requirements.txt` (still
  present in the new folder, superseded by `pyproject.toml`; safe to delete).

## Migration to the template (done 2026-11-07)

- [x] Code moved into `src/mtgmate_checker/` (`main.py` -> `cli.py` with a `mtgmate-checker`
      console script and `python -m mtgmate_checker`); imports rewritten; `helpers/` is now a
      real package.
- [x] ruff, ruff format and mypy `--strict` pass on `src/` and `tests/`; all 12 pre-commit hooks
      pass (checked with `pre-commit run --files ...`). No commits exist yet in this repo.
- [x] Tests: shared fakes moved to `tests/support.py`; test classes no longer inherit from each
      other (that silently re-ran tests: 140 reported -> 102 real tests, all 99 old unique
      tests kept, 3 new). New: `test_smoke.py` from the template.
- [ ] Delete the old `requirements.txt` (superseded by `pyproject.toml`); retire the old
      `MTGMate/` folder once happy (note both hold a copy of `mtgmate_cookies.json`).
- [ ] Decide on the Claude commit-message hook (template option was declined).


### 2026-11-07 — Template migration: bugs found by the type checker
- `ui/app.py` defined `_log(self, text)`, which silently overrode Textual's own internal
  `App._log(group, verbosity, ...)` (a crash waiting for any Textual internal logging). Renamed
  to `_write_log`.
- `ui/settings.py`: typed (`Settings` TypedDict); saved values are only used if they have the
  right type, so a corrupt/hand-edited settings file can't inject a wrong type.
- Removed a test-only `AttributeError` fallback from `run_check` (fakes now have the attribute).
- On-screen hints now say `mtgmate-checker login` (the old `python main.py login` is gone).
