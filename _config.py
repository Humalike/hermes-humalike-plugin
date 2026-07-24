"""Shared Humalike API config: one URL + one key for every sub-plugin."""

from __future__ import annotations

import os


DEFAULT_API = "https://api.humalike.com"


def _getenv(name: str, default: str = "") -> str:
    """Env var, honoring Hermes's per-profile secret scope when one is in
    effect (multiplexed turns read the profile's .env, never another
    profile's os.environ). Outside Hermes or single-profile → plain getenv."""
    try:
        from agent.secret_scope import get_secret

        val = get_secret(name, default)
        return val if val is not None else default
    except Exception:
        return os.getenv(name, default)


def service_url() -> str:
    """Base URL for all Humalike calls (``HUMALIKE_API_URL``). Defaults to the
    public API so a fresh install needs zero env setup; set it EMPTY
    (``HUMALIKE_API_URL=``) to explicitly disable turn-taking."""
    return _getenv("HUMALIKE_API_URL", DEFAULT_API).rstrip("/")


def api_key() -> str:
    """API key for all Humalike calls, sent as ``Authorization: Bearer`` (``HUMALIKE_API_KEY``)."""
    return _getenv("HUMALIKE_API_KEY", "")
