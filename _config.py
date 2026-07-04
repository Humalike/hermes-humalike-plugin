"""Shared Humalike API config: one URL + one key for every sub-plugin."""

from __future__ import annotations

import os


DEFAULT_API = "https://api.humalike.com"


def service_url() -> str:
    """Base URL for all Humalike calls (``HUMALIKE_API_URL``). Defaults to the
    public API so a fresh install needs zero env setup; set it EMPTY
    (``HUMALIKE_API_URL=``) to explicitly disable turn-taking."""
    return os.getenv("HUMALIKE_API_URL", DEFAULT_API).rstrip("/")


def api_key() -> str:
    """API key for all Humalike calls, sent as ``Authorization: Bearer`` (``HUMALIKE_API_KEY``)."""
    return os.getenv("HUMALIKE_API_KEY", "")
