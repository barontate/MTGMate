from __future__ import annotations

import contextlib
import os
import sys
import threading
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult, SuspendNotSupported
from textual.binding import BindingType
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Footer, Header, Input, Label, ProgressBar, RichLog

from mtgmate_checker.helpers import config
from mtgmate_checker.helpers.auth import saved_login_time
from mtgmate_checker.helpers.check import run_check, run_login
from mtgmate_checker.helpers.collection import summarize_collection
from mtgmate_checker.ui import settings
from mtgmate_checker.ui.settings import Settings


def output_path(text: str) -> Path:
    """Where to write results: blank -> the default name; '.csv' added if missing.
    Relative names are anchored to the project folder."""
    name = text.strip().strip('"') or settings.DEFAULT_OUTPUT_NAME
    path = Path(name)
    if path.suffix.lower() != ".csv":
        path = path.with_name(path.name + ".csv")
    return path if path.is_absolute() else config.PROJECT_ROOT / path


def parse_spare(text: str) -> int:
    """Spare copies to keep per card; blank / invalid / negative -> 0."""
    try:
        return max(0, int(text))
    except ValueError:
        return 0


def parse_parallel(text: str) -> int:
    """Number of parallel connections, clamped to 1..MAX_PARALLEL_LIMIT."""
    try:
        value = int(text)
    except ValueError:
        return config.MAX_PARALLEL_SETS
    return max(1, min(value, MAX_PARALLEL_LIMIT))


MAX_PARALLEL_LIMIT = 8
EXIT_GRACE_SECONDS = 8.0  # how long quitting mid-check may wait before forcing exit


class CartFullScreen(ModalScreen[bool]):
    """Shown when the cart hits mtgmate's order limit. Dismisses True to continue."""

    DEFAULT_CSS = """
    CartFullScreen { align: center middle; }
    #cart-dialog { width: 64; height: auto; padding: 1 2; border: thick $warning;
                   background: $surface; }
    #cart-dialog Label { width: 100%; margin-bottom: 1; }
    #cart-buttons { height: 3; }
    #cart-buttons Button { margin-right: 1; }
    """

    def __init__(self, count: int, limit: int) -> None:
        super().__init__()
        self._count, self._limit = count, limit

    def compose(self) -> ComposeResult:
        with Vertical(id="cart-dialog"):
            yield Label(f"Buylist cart is full ({self._count}/{self._limit} cards).")
            yield Label(
                "Submit this order on mtgmate, then press Continue to keep adding "
                "cards to a new one. The check is paused until you choose."
            )
            with Horizontal(id="cart-buttons"):
                yield Button("Continue", variant="success", id="cart-continue")
                yield Button("Stop here", variant="error", id="cart-stop")

    @on(Button.Pressed, "#cart-continue")
    def _continue(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cart-stop")
    def _stop(self) -> None:
        self.dismiss(False)


class MTGMateApp(App[None]):
    TITLE = "MTGMate Buylist Checker"
    CSS = """
    Screen { padding: 0 1; }
    #login-row { height: 3; }
    #login-status { padding: 1 1; width: 1fr; }
    #csv-status { padding: 0 1; height: auto; }
    #csv-status.ok { color: $success; }
    #csv-status.bad { color: $error; }
    #form { height: auto; }
    #spare-row { height: 3; }
    #spare { width: 12; }
    #spare-label { padding: 1 1; }
    #parallel-row { height: 3; }
    #parallel { width: 12; }
    #parallel-label { padding: 1 1; }
    #status-row { height: 1; margin-top: 1; }
    #progress { width: 1fr; }
    #speed { width: 28; text-align: right; }
    #log { height: 1fr; border: round $primary; margin: 1 0; }
    #buttons { height: 3; }
    #buttons Button { margin-right: 1; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        ("ctrl+l", "login", "Log in"),
        ("ctrl+r", "run", "Run"),
        ("ctrl+x", "stop", "Stop"),
        ("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._cancel = threading.Event()
        self._running = False
        self._check_done = threading.Event()  # set whenever no check thread is running
        self._check_done.set()
        self._saved = settings.load()

    # -- layout -----------------------------------------------------------

    def compose(self) -> ComposeResult:
        s = self._saved
        yield Header()
        with Horizontal(id="login-row"):
            yield Label("", id="login-status")
            yield Button("Log in", id="login")
        with Vertical(id="form"):
            yield Input(
                s["collection_path"], placeholder="ManaBox collection .csv path", id="collection"
            )
            yield Label("", id="csv-status")
            yield Input(
                s["output_path"],
                placeholder=f"Output file name (default: {settings.DEFAULT_OUTPUT_NAME})",
                id="output",
            )
            yield Checkbox("Add to Buylist as cards are checked", s["add_to_cart"], id="add")
            with Horizontal(id="spare-row"):
                yield Input(str(s["spare"]), type="integer", id="spare")
                yield Label(
                    "Number of cards to keep spare (subtract from maximum selling amount)",
                    id="spare-label",
                )
            with Horizontal(id="parallel-row"):
                yield Input(str(s["parallel"]), type="integer", id="parallel")
                yield Label(
                    "Parallel connections (1 = one set at a time, max 8)", id="parallel-label"
                )
            yield Checkbox(
                "Polite mode: honour mtgmate's robots.txt 60s crawl-delay (one request a minute, "
                "one at a time - very slow)",
                s["polite"],
                id="polite",
            )
            yield Checkbox("Remember these settings for next time", s["remember"], id="remember")
        with Horizontal(id="status-row"):
            yield ProgressBar(total=100, show_eta=False, id="progress")
            yield Label("", id="speed")
        yield RichLog(id="log", wrap=True, markup=False, max_lines=2000)
        with Horizontal(id="buttons"):
            yield Button("Run", variant="success", id="run")
            yield Button("Stop", variant="error", id="stop", disabled=True)
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_login_status()
        self._refresh_csv_status()
        if saved_login_time() is None:
            self._write_log(
                "Not logged in yet: press 'Log in' (ctrl+l) to sign in to mtgmate first."
            )

    # -- login ------------------------------------------------------------

    def _refresh_login_status(self) -> None:
        when = saved_login_time()
        label = self.query_one("#login-status", Label)
        if when is None:
            label.update("[b red]Not logged in[/] - press 'Log in' to sign in to mtgmate")
        else:
            label.update(
                f"[b green]Logged in[/] (saved {when:%Y-%m-%d %H:%M}) - "
                "re-log in if a check says it expired"
            )

    @on(Button.Pressed, "#login")
    def action_login(self) -> None:
        if self.query_one("#login", Button).disabled:
            return
        # The login flow opens a browser window and prompts in the console, so hand
        # the terminal over to it, then come back.
        ok, error = False, ""
        try:
            with self.suspend():
                print("\n=== mtgmate login ===")
                try:
                    ok = run_login()
                except ImportError:
                    error = (
                        "Playwright isn't installed. Run: pip install playwright && "
                        "playwright install firefox"
                    )
                except Exception as e:
                    error = f"Login failed: {type(e).__name__}: {e}"
                if error:
                    print(error)
                input("\nPress Enter to return to the app...")
        except SuspendNotSupported:
            error = "This terminal can't hand over to the login browser. Run: mtgmate-checker login"
        self._refresh_login_status()
        self._write_log(
            error or ("Login saved." if ok else "Login was not completed; nothing saved.")
        )

    # -- collection feedback ----------------------------------------------

    def _refresh_csv_status(self) -> None:
        summary = summarize_collection(self.query_one("#collection", Input).value)
        label = self.query_one("#csv-status", Label)
        label.update(
            Text(("OK  " if summary.ok else "!!  ") + summary.message)
        )  # Text: no markup parsing
        label.set_class(summary.ok, "ok")
        label.set_class(not summary.ok, "bad")

    @on(Input.Changed, "#collection")
    def _collection_changed(self) -> None:
        self._refresh_csv_status()

    # -- actions ----------------------------------------------------------

    @on(Button.Pressed, "#run")
    def action_run(self) -> None:
        if self.query_one("#run", Button).disabled:
            return
        collection = self.query_one("#collection", Input).value.strip().strip('"')
        summary = summarize_collection(collection)
        if not summary.ok:
            self._write_log(f"Can't run: {summary.message}")
            return

        self._cancel.clear()
        self._check_done.clear()
        self._set_running(True)
        self.query_one("#progress", ProgressBar).update(progress=0)
        self.query_one("#speed", Label).update("")
        self._check(
            Path(collection),
            output_path(self.query_one("#output", Input).value),
            self.query_one("#add", Checkbox).value,
            parse_parallel(self.query_one("#parallel", Input).value),
            self.query_one("#polite", Checkbox).value,
            parse_spare(self.query_one("#spare", Input).value),
        )

    @on(Button.Pressed, "#stop")
    def action_stop(self) -> None:
        # Cooperative: run_check stops at the next card and logs "Cancelled."
        self._cancel.set()

    @work(thread=True, exclusive=True)
    def _check(
        self,
        collection: Path,
        output: Path,
        add_to_cart: bool,
        parallel: int,
        polite: bool,
        spare: int,
    ) -> None:
        """Runs in a worker thread; every UI touch goes through call_from_thread."""
        ui = self.call_from_thread
        try:
            run_check(
                csv_path=collection,
                output=output,
                log_update=lambda text: ui(self._write_log, text),
                progress_update=lambda pct: ui(self._set_progress, pct),
                speed_update=lambda rate: ui(self._set_speed, rate),
                should_cancel=self._cancel.is_set,
                add_to_cart=add_to_cart,
                max_workers=parallel,
                polite=polite,
                spare=spare,
                on_cart_full=self._ask_cart_full,
            )
        except Exception as e:  # never let an exception die silently in a thread
            ui(self._write_log, f"Check failed: {type(e).__name__}: {e}")
        finally:
            self._check_done.set()  # before the UI call, which fails once the app is gone
            ui(self._set_running, False)

    def _ask_cart_full(self, count: int) -> bool:
        """Worker-thread callback: show the dialog and block until the user answers
        (or presses Stop). Returns True to keep going."""
        answer: dict[str, bool] = {}
        done = threading.Event()
        screen = CartFullScreen(count, config.CART_LIMIT)

        def on_answer(value: bool | None) -> None:
            answer["ok"] = bool(value)
            done.set()

        def show() -> None:
            self.bell()  # get the user's attention; this can sit for a while
            self.push_screen(screen, on_answer)

        self.call_from_thread(show)
        while not done.wait(0.25):
            if self._cancel.is_set():
                self.call_from_thread(screen.dismiss, False)
                return False
        return answer.get("ok", False)

    # -- UI updates (main thread only) -------------------------------------

    def _write_log(self, text: str) -> None:
        self.query_one("#log", RichLog).write(text)

    def _set_progress(self, pct: int) -> None:
        self.query_one("#progress", ProgressBar).update(progress=pct)

    def _set_speed(self, rate: float) -> None:
        self.query_one("#speed", Label).update(f"{rate:.2f} items per second")

    def _set_running(self, running: bool) -> None:
        self._running = running
        self.query_one("#run", Button).disabled = running
        self.query_one("#stop", Button).disabled = not running

    # -- settings ---------------------------------------------------------

    def _current_settings(self) -> Settings:
        spare = self.query_one("#spare", Input).value
        return {
            "remember": self.query_one("#remember", Checkbox).value,
            "collection_path": self.query_one("#collection", Input).value,
            "output_path": self.query_one("#output", Input).value,
            "add_to_cart": self.query_one("#add", Checkbox).value,
            "spare": parse_spare(spare),
            "parallel": parse_parallel(self.query_one("#parallel", Input).value),
            "polite": self.query_one("#polite", Checkbox).value,
        }

    @on(Checkbox.Changed, "#polite")
    def _polite_toggled(self, event: Checkbox.Changed) -> None:
        # Polite mode is strictly one connection, so the parallel box doesn't apply.
        self.query_one("#parallel", Input).disabled = event.value

    @on(Input.Changed)
    @on(Checkbox.Changed)
    def _persist(self) -> None:
        """Save on every change (cheap, and survives crashes). Widgets may not all
        exist yet while the screen is still being composed."""
        with contextlib.suppress(NoMatches):
            settings.save(self._current_settings())

    def on_unmount(self) -> None:
        self._cancel.set()  # let a running check wind down
        if not self._check_done.is_set():
            # Textual runs the check on asyncio's default thread pool, and Python waits
            # for those threads before the process can exit. Everything polls the cancel
            # flag, so this normally takes a moment; but if a network call is stuck, don't
            # leave the terminal frozen. Results are flushed per match, so a hard exit
            # loses nothing.
            watchdog = threading.Timer(EXIT_GRACE_SECONDS, self._force_exit)
            watchdog.daemon = True
            watchdog.start()

    def _force_exit(self) -> None:
        if self._check_done.is_set():
            return  # the check finished in time; nothing to force
        print("Still waiting on a network request to finish - exiting now.", file=sys.stderr)
        os._exit(0)
