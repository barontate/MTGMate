hey babes ✨

this checks your card collection against mtgmate's buylist, tells you which cards they want to buy, and puts them in your buylist cart for you. you then open mtgmate in your normal browser, remove anything you don't want to sell, and complete the sale.

it runs in the terminal (no pop-up window), so don't panic when it looks like a hacker movie.


## first-time setup

you need [uv](https://docs.astral.sh/uv/) (it installs the right Python and everything else). in the MTGMate-checker folder:

```
uv sync
uv run playwright install firefox
```

(the second line is only for logging in. it downloads a copy of Firefox for the tool to use.)

on Windows PowerShell you can also activate the environment with `.venv\Scripts\Activate.ps1`, then drop the `uv run` from the commands below.


## logging in (once in a while)

the tool needs to be logged in to mtgmate as you. it never sees your password - you type it into a browser.

1. start the app: `uv run mtgmate-checker`
2. press the **Log in** button (or `ctrl+l`)
3. a Firefox window opens on mtgmate's sign-in page. type your email and password, solve the captcha if it shows one, and click Sign In
4. go back to the terminal and press Enter

the top of the app then says **Logged in**. that login is saved in `mtgmate_cookies.json` and reused until it expires (the app will tell you if it has - just log in again). you can also log in without opening the app: `uv run mtgmate-checker login`.

the captcha sometimes catches the bot. if it does, just try again, it usually works the second time.


## running a check

start the app with `uv run mtgmate-checker`, then fill in:

- **collection path** - your ManaBox collection export (.csv). tip: in Windows, right-click the file (or shift + right-click on older versions) → "Copy as path", then paste it in. as you type, a line under the box tells you if it's the right file (e.g. `OK  wants.csv: 1305 cards (1410 copies) across 214 sets`) or what's wrong with it (not found, missing columns, etc). you can't run until it says OK
- **output file name** - where to save the results. leave it blank and it saves as `mtg wants.csv` in the MTGMate folder
- **Add to Buylist as cards are checked** - on: matching cards go into your mtgmate cart. off: it only writes the results file and doesn't touch your cart (a dry run)
- **keep spare** - how many of each card to hold back. for example 1 means "keep one of each card and sell the rest". if you have the same card on several rows it keeps 1 in total, not 1 per row
- **parallel connections** - how many sets it checks at once (1-8, default 4). higher is faster
- **polite mode** - see below
- **Remember these settings for next time** - saves everything above (not your login) to `mtgmate_settings.json` and fills it back in next time you open the app. untick it and the saved settings are deleted

then press **Run** (or `ctrl+r`). there's a progress bar, a speed readout and a log. press **Stop** (or `ctrl+x`) any time - it stops at the next card. `ctrl+q` quits.

when it's done, open mtgmate in your normal browser (the cart is tied to your account, so it'll be there) and have a look through before you complete the sale. compare against the results file if you like.


## the results file

one row per card mtgmate wants:

| column | what it is |
| --- | --- |
| card_name | the card (with things like "(Borderless 123)" where it matters) |
| set_code, finish | which set and Nonfoil / Foil / Etched |
| credit_offered | store credit per card |
| mtgmate_wants | how many mtgmate is buying |
| you_own | how many you actually own |
| uuid | mtgmate's id for that card |
| to_sell | how many it put in the cart: what you own minus your spare, capped at what mtgmate wants |

duplicate rows in your collection are combined first, so you'll never end up with more in the cart than mtgmate actually wants. cards with a quantity of 0 are skipped.


## the 300 card limit

mtgmate's buylist order is capped at 300 cards (total quantity). the tool reads how many are in your cart before it starts and never goes over. when the cart is full it stops and shows a box:

> Buylist cart is full (300/300 cards). Submit this order on mtgmate, then press Continue...

submit that order on mtgmate, then press **Continue** and it carries on with a fresh cart. press **Stop here** to end the run instead. (a card that only half-fits gets split across the two orders.) the check waits as long as you need.


## polite mode (very slow, very considerate)

mtgmate's robots.txt asks automated visitors to wait 60 seconds between requests. normally the tool goes a lot faster than that (a few requests a second at most). tick **Polite mode** and it does exactly what the file asks: one request a minute, one at a time. a big collection can take hours, so the log tells you the estimate up front, and Stop still works instantly. your cart adds are slowed down too.

technically mtgmate's notes say bot trawling isn't allowed, but they haven't seemed to block it. use it at your own risk i guess? polite mode is there for when you'd rather not risk it. if the log ever says the site asked it to slow down, that's mtgmate pushing back - lower the parallel connections or use polite mode.


## if something's off

- **"Not logged in" / "login has expired"** - press Log in again.
- **check your setup works** - `uv run mtgmate-checker diagnose` makes two read-only requests (it never touches your cart) and tells you if you're logged in and what it thinks is in your cart. if the cart number looks wrong, send me the output.
- **Log in button says the terminal can't hand over** - run `uv run mtgmate-checker login` instead.
- **"Playwright isn't installed"** - run the two commands under first-time setup.
- **quitting seems stuck** - give it a few seconds. if a request is stuck it exits by itself after about 8 seconds.
- **a card you expected didn't match** - it matches on name + set + finish, so a foil you own won't match a non-foil listing. cards with variants ("Borderless", "Showcase" etc) are worked out via Scryfall, so make sure your export has the `Scryfall ID` column.


## for whoever is poking at the code

```bash
uv run pytest                         # offline tests (no network needed)
uv run pre-commit run --all-files     # lint, format, type check, secrets scan
uv add <package>                      # add a dependency
```

- `CLAUDE.md` has the architecture, a log of what's changed, and the to-do list
- secrets that must never be shared or committed: `mtgmate_cookies.json` (a live login) and `mtgmate_settings.json` (both are git-ignored)
- project created from [barontate/python-template](https://github.com/barontate/python-template) with copier; `uvx copier update --trust` pulls in template updates

## Windows executable

Download `mtgmate-checker-<version>.exe` from the [Releases](https://github.com/barontate/MTGMate/releases)
page and run it from a terminal (or double-click it). Cookies, settings and result CSVs are saved
in the folder that contains the `.exe`. The first **Log in** downloads Firefox for the sign-in window.

To build it yourself: `bash scripts/build_exe.sh` (output: `dist/mtgmate-checker.exe`).
