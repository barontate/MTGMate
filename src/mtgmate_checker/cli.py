"""Command-line entry point: `mtgmate-checker [login|diagnose]` (no argument opens the app)."""

from __future__ import annotations

import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    command = args[0] if args else ""

    if command == "login":
        from mtgmate_checker.helpers.check import run_login

        sys.exit(0 if run_login() else 1)

    if command == "diagnose":
        from mtgmate_checker.helpers.check import run_diagnose

        sys.exit(0 if run_diagnose() else 1)

    from mtgmate_checker.ui.app import MTGMateApp

    MTGMateApp().run()


if __name__ == "__main__":
    main()
