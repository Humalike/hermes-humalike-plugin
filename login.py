#!/usr/bin/env python3
"""Terminal device-authorization login — the install-time cousin of /connect.

Run it right after installing the plugin (stdlib only — no Hermes venv, no
httpx):

    python3 ~/.hermes/plugins/humalike/login.py

Prints the approval URL (and opens a browser tab when the machine has one — on
SSH/headless boxes open the printed link on your phone), polls until the link
is approved, then writes HUMALIKE_API_KEY into ``~/.hermes/.env``.

Three callers share this module:
  * the terminal (``__main__``) — install-time login,
  * ``register()`` via :func:`maybe_first_boot_login` — pops the login once on
    the first keyless gateway boot (marker-guarded),
  * ``connect.py`` — reuses :func:`poll_session` and :func:`write_env_key`.

Config is read from the process environment first, then ``~/.hermes/.env`` —
the installer writes the gateway key to the file before any shell exports it.
"""

from __future__ import annotations

import json
import os
import platform
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

HERMES_ENV = Path.home() / ".hermes" / ".env"
DEFAULT_API = "https://api.humalike.com"
CREATE = "/v1/keys/actions/cli_create"
POLL = "/v1/keys/actions/cli_poll"

# RFC 8628 public client identifier for the CLI lane: it names the client (a
# Hermes plugin install) to the API and unlocks ONLY cli_create/cli_poll — the
# per-session device_code stays the sole credential that can claim a key, so
# this ships in the open like any OAuth public client_id.
# ponytail: no baked default yet — set HUMALIKE_CLI_GATEWAY_KEY until the
# published identifier lands here in a follow-up.
GATEWAY_KEY_DEFAULT = ""

_MARKER = Path.home() / ".hermes" / ".turn_taking_login_prompted"


# ── Config (env first, then ~/.hermes/.env) ───────────────────────────────────
def read_env_file(path: Path | None = None) -> dict:
    """``KEY=VALUE`` lines (just the subset we need — no quoting/expansion)."""
    try:
        lines = (path or HERMES_ENV).read_text().splitlines()
    except OSError:
        return {}
    out = {}
    for ln in lines:
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, _, v = ln.partition("=")
            out[k.strip()] = v.strip()
    return out


def cfg(name: str, default: str = "") -> str:
    return os.getenv(name) or read_env_file().get(name, "") or default


def gateway_key() -> str:
    return cfg("HUMALIKE_CLI_GATEWAY_KEY", GATEWAY_KEY_DEFAULT)


def api_url() -> str:
    return cfg("HUMALIKE_API_URL", DEFAULT_API).rstrip("/")


# ── Key persistence ───────────────────────────────────────────────────────────
def write_env_key(path: Path, key: str) -> None:
    """Upsert ``HUMALIKE_API_KEY=…``, preserving every other line. A fresh file
    is created 0600 from the first byte (no world-readable window)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text().splitlines() if path.exists() else []
    lines = [ln for ln in lines if not ln.startswith("HUMALIKE_API_KEY=")]
    lines.append(f"HUMALIKE_API_KEY={key}")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    try:
        path.chmod(0o600)  # a pre-existing file keeps its old mode otherwise
    except Exception:
        pass


# ── The device-auth API (sync, stdlib) ────────────────────────────────────────
def post(path: str, body: dict, bearer: str) -> dict:
    req = urllib.request.Request(
        api_url() + path,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310 — our own API URL
        return json.load(r)


def create_session(bearer: str) -> dict:
    return post(CREATE, {"client": "hermes", "hostname": socket.gethostname(),
                         "os": platform.system()}, bearer)


def poll_session(session: dict, bearer: str) -> dict:
    """Blocking poll until a terminal status; returns the final poll body
    (``{'status': 'expired'}`` synthesized on TTL). Any HTTP error is transient
    by contract — keep polling; the session TTL is the real deadline. Unknown
    future statuses are treated as pending."""
    interval = max(1, int(session.get("interval", 3)))
    deadline = time.monotonic() + int(session.get("expires_in", 600))
    while time.monotonic() < deadline:
        time.sleep(interval)
        try:
            data = post(POLL, {"device_code": session["device_code"]}, bearer)
        except Exception:
            continue
        if data.get("status") in ("authorized", "denied", "expired"):
            return data
    return {"status": "expired"}


# ── Terminal flow ─────────────────────────────────────────────────────────────
def run() -> int:
    """0 = key saved (or already present), 1 = failed/denied/expired."""
    if cfg("HUMALIKE_API_KEY"):
        print("Already connected — HUMALIKE_API_KEY is set.")
        return 0
    bearer = gateway_key()
    if not bearer:
        print("No HUMALIKE_CLI_GATEWAY_KEY configured — create an API key at "
              f"https://humalike.com and put HUMALIKE_API_KEY=… in {HERMES_ENV} instead.")
        return 1
    try:
        session = create_session(bearer)
    except Exception as e:
        print(f"Could not reach Humalike to start the login ({e}).")
        return 1

    uri = session["verification_uri"]
    minutes = max(1, int(session.get("expires_in", 600)) // 60)
    print("\nOpen this link on any device and approve to connect your Humalike account:\n"
          f"\n    {uri}\n"
          f"\nWaiting for approval (link valid ~{minutes} min, Ctrl-C to abort)…")
    try:
        import webbrowser

        webbrowser.open(uri)  # pops a tab on a desktop; harmless no-op headless
    except Exception:
        pass

    data = poll_session(session, bearer)
    status = data.get("status")
    if status == "authorized":
        key = data.get("api_key") or ""
        write_env_key(HERMES_ENV, key)
        os.environ["HUMALIKE_API_KEY"] = key  # live for this process too
        who = (data.get("account") or {}).get("email") or "your account"
        print(f"✅ Connected as {who} — key saved to {HERMES_ENV}.")
        return 0
    print(f"Login {status} — run this script again (or send the bot /connect) to retry.")
    return 1


# ── First-boot popup (called from register()) ─────────────────────────────────
def maybe_first_boot_login() -> None:
    """Pop the login once, on the first keyless gateway boot: a browser tab on
    a desktop, the printed URL on the gateway console otherwise. Runs on a
    daemon thread so boot never blocks on the ~10-minute approval window.

    Marker written when the prompt FIRES, not on success — an ignored tab must
    not reopen on every boot. Retry paths: delete the marker, rerun login.py,
    or send /connect."""
    if cfg("HUMALIKE_API_KEY") or not gateway_key() or _MARKER.exists():
        return
    try:
        _MARKER.parent.mkdir(parents=True, exist_ok=True)
        _MARKER.write_text("1")
    except OSError:
        pass  # best-effort; worst case the prompt repeats next boot
    threading.Thread(target=run, daemon=True, name="humalike-login").start()


if __name__ == "__main__":
    sys.exit(run())
