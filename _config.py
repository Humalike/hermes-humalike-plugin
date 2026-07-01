"""Shared Humalike API config: one URL + one key for every sub-plugin."""

from __future__ import annotations

import os


def service_url() -> str:
    """Base URL for all Humalike calls (``HUMALIKE_API_URL``)."""
    return os.getenv("HUMALIKE_API_URL", "").rstrip("/")


def api_key() -> str:
    """API key for all Humalike calls, sent as ``Authorization: Bearer`` (``HUMALIKE_API_KEY``)."""
    return os.getenv("HUMALIKE_API_KEY", "")
