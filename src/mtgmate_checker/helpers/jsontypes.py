"""Small shared type aliases for JSON-ish data coming from the web."""

from __future__ import annotations

from typing import Any

# A decoded JSON object. The shape comes from a third-party site, so values stay `Any`
# and are validated where they are used.
JsonDict = dict[str, Any]
