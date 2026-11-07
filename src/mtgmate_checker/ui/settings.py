"""Tiny JSON-backed store for the "Remember" feature (replaces Qt's QSettings)."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any, TypedDict, cast

from mtgmate_checker.helpers import config

SETTINGS_PATH = config.PROJECT_ROOT / "mtgmate_settings.json"

DEFAULT_OUTPUT_NAME = "mtg wants.csv"


class Settings(TypedDict):
    """Everything the UI remembers between runs."""

    remember: bool
    collection_path: str
    output_path: str
    add_to_cart: bool
    spare: int
    parallel: int
    polite: bool


DEFAULTS: Settings = {
    "remember": False,
    "collection_path": "",
    "output_path": DEFAULT_OUTPUT_NAME,
    "add_to_cart": True,
    "spare": 0,
    "parallel": config.MAX_PARALLEL_SETS,
    "polite": False,
}


def load(path: Path = SETTINGS_PATH) -> Settings:
    """Saved values merged over defaults. Only restored if 'remember' was ticked.

    A saved value is only used if it has the same type as its default, so a corrupt or
    hand-edited file can't put a string where a number belongs.
    """
    try:
        saved: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cast(Settings, dict(DEFAULTS))
    if not isinstance(saved, dict) or saved.get("remember") is not True:
        return cast(Settings, dict(DEFAULTS))
    merged: dict[str, Any] = dict(DEFAULTS)
    for key, default in DEFAULTS.items():
        if key in saved and type(saved[key]) is type(default):
            merged[key] = saved[key]
    return cast(Settings, merged)


def save(values: Settings, path: Path = SETTINGS_PATH) -> None:
    """Persist values if 'remember' is ticked, otherwise wipe the file."""
    # Settings are a convenience; never crash the app over them.
    with contextlib.suppress(OSError):
        if values["remember"]:
            path.write_text(json.dumps(values, indent=2), encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
