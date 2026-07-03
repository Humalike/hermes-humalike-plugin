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
import logging
import os
import platform
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

_log = logging.getLogger(__name__)

def _hermes_home() -> Path:
    """The real hermes home: the host's single source of truth when importable
    (HERMES_HOME-aware, platform-native defaults), else the env var, else
    ``~/.hermes`` (login.py run standalone outside the hermes venv)."""
    try:
        from hermes_constants import get_hermes_home  # noqa: PLC0415

        return Path(get_hermes_home())
    except Exception:
        return Path(os.getenv("HERMES_HOME") or "~/.hermes").expanduser()


HERMES_ENV = _hermes_home() / ".env"
DEFAULT_API = "https://api.humalike.com"
CREATE = "/v1/keys/actions/cli_create"
POLL = "/v1/keys/actions/cli_poll"

# RFC 8628 public client identifier for the CLI lane: it names the client (a
# Hermes plugin install) to the API and unlocks ONLY cli_create/cli_poll — the
# per-session device_code stays the sole credential that can claim a key, so
# this ships in the open like any OAuth public client_id. Override with
# HUMALIKE_CLI_GATEWAY_KEY (e.g. for staging).
GATEWAY_KEY_DEFAULT = "hcg_360rQLmr4iabWKiEqc5ZFXY5sUM8g-wTjFO3cwNgTlI"

_MARKER = _hermes_home() / ".turn_taking_login_prompted"

# The not-yet-approved link, while a login (first-boot or /connect) is in
# flight — None when idle. Lets /connect RE-SHOW the pending link instead of
# starting a duplicate session (a TUI banner can paint over the first print).
PENDING_URI = None


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


# ── TUI-safe printing ─────────────────────────────────────────────────────────
def _show(text: str) -> None:
    """Print so it survives the hermes TUI. Under a running prompt_toolkit
    application a bare print() is erased on the next redraw — route through
    run_in_terminal (hermes cli.py's own idiom), which suspends the prompt,
    prints above it into scrollback, and resumes. Plain print everywhere else
    (gateway console, bare terminal, prompt_toolkit not installed)."""
    try:
        from prompt_toolkit.application import get_app_or_none, run_in_terminal

        app = get_app_or_none()
        if app is not None and app.is_running and getattr(app, "loop", None) is not None:
            import asyncio

            app.loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(run_in_terminal(lambda: print(text)))
            )
            return
    except Exception:
        pass  # fall through to the plain print
    print(text)


def _wait_for_tui(timeout: float = 8.0) -> None:
    """Hold the FIRST link display until the hermes TUI has painted, so it
    prints through run_in_terminal right below the banner instead of being
    buried above it. Returns fast when no TUI is coming: immediately when
    prompt_toolkit is absent (bare console), on timeout in the gateway."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            from prompt_toolkit.application import get_app_or_none
        except Exception:
            return
        app = get_app_or_none()
        if app is not None and app.is_running:
            time.sleep(0.3)  # let the banner finish painting
            return
        time.sleep(0.2)


# ── Key persistence ───────────────────────────────────────────────────────────
def upsert_env(path: Path, updates: dict) -> None:
    """Upsert ``KEY=VALUE`` lines, preserving every other line (comments
    included). A fresh file is created 0600 from the first byte (no
    world-readable window — it can hold a live credential)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text().splitlines() if path.exists() else []
    lines = [ln for ln in lines if ln.split("=", 1)[0].strip() not in updates]
    lines += [f"{k}={v}" for k, v in updates.items()]
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    try:
        path.chmod(0o600)  # a pre-existing file keeps its old mode otherwise
    except Exception:
        pass


def write_env_key(path: Path, key: str) -> None:
    """Upsert the API key (see :func:`upsert_env`)."""
    upsert_env(path, {"HUMALIKE_API_KEY": key})


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


def poll_session(session: dict, bearer: str, on_wait=None) -> dict:
    """Blocking poll until a terminal status; returns the final poll body
    (``{'status': 'expired'}`` synthesized on TTL). Any HTTP error is transient
    by contract — keep polling; the session TTL is the real deadline. Unknown
    future statuses are treated as pending. ``on_wait(elapsed_seconds)`` is
    called once per loop — the terminal flow uses it to re-print the link."""
    interval = max(1, int(session.get("interval", 3)))
    start = time.monotonic()
    deadline = start + int(session.get("expires_in", 600))
    while time.monotonic() < deadline:
        time.sleep(interval)
        if on_wait is not None:
            try:
                on_wait(time.monotonic() - start)
            except Exception:
                pass
        try:
            data = post(POLL, {"device_code": session["device_code"]}, bearer)
        except Exception:
            continue
        if data.get("status") in ("authorized", "denied", "expired"):
            return data
    return {"status": "expired"}


# ── Terminal flow ─────────────────────────────────────────────────────────────
def run(wait_for_tui: bool = False) -> int:
    """0 = key saved (or already present), 1 = failed/denied/expired.

    ``wait_for_tui`` (the first-boot path inside hermes): delay the first
    link display until the TUI is up, so the link lands BELOW the banner —
    a pre-banner print just scrolls out of sight."""
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
        _show(f"Could not reach Humalike to start the login ({e}).")
        return 1

    uri = session["verification_uri"]
    minutes = max(1, int(session.get("expires_in", 600)) // 60)
    # Log first (always findable), open the browser ASAP (desktop case), and
    # only THEN display — after the TUI is up when asked, so the link prints
    # below the banner via run_in_terminal instead of scrolling away above it.
    _log.warning("humalike login: approve at %s (valid ~%d min)", uri, minutes)
    try:
        import webbrowser

        webbrowser.open(uri)  # pops a tab on a desktop; harmless no-op headless
    except Exception:
        pass
    if wait_for_tui:
        _wait_for_tui()
    _show("\n🔗 Humalike plugin — one step left: connect your account.\n"
          "   Open this link on any device (your phone works) and approve:\n"
          f"\n    {uri}\n"
          f"\nWaiting for approval (link valid ~{minutes} min)…\n")

    # Gentle nudges while pending; /connect also re-shows PENDING_URI on demand.
    reminders = {60, 180}

    def _remind(elapsed: float) -> None:
        due = {t for t in reminders if t <= elapsed}
        if due:
            reminders.difference_update(due)
            _show(f"\n⏳ Humalike login still waiting — approve at: {uri}\n")

    global PENDING_URI
    PENDING_URI = uri
    try:
        data = poll_session(session, bearer, on_wait=_remind)
    finally:
        PENDING_URI = None
    status = data.get("status")
    if status == "authorized":
        key = data.get("api_key") or ""
        write_env_key(HERMES_ENV, key)
        os.environ["HUMALIKE_API_KEY"] = key  # live for this process too
        who = (data.get("account") or {}).get("email") or "your account"
        _log.warning("humalike login: connected as %s", who)
        _show(f"\n✅ Connected as {who} — key saved to {HERMES_ENV}.\n")
        return 0
    _log.warning("humalike login: %s", status)
    _show(f"\nLogin {status} — send the bot /connect (or rerun login.py) to retry.\n")
    return 1


# ── First-boot popup (called from register()) ─────────────────────────────────
def maybe_first_boot_login() -> None:
    """Pop the login once, on the first keyless gateway boot: a browser tab on
    a desktop, the printed URL on the gateway console otherwise. Runs on a
    daemon thread so boot never blocks on the ~10-minute approval window.

    Marker written when the prompt FIRES, not on success — an ignored tab must
    not reopen on every boot. Retry paths: delete the marker, rerun login.py,
    or send /connect."""
    if cfg("HUMALIKE_API_KEY"):
        return  # already connected
    if not gateway_key():
        _log.info("humalike login: skipped — no client identifier configured")
        return
    if _MARKER.exists():
        _log.info("humalike login: already prompted once (delete %s to retry, or send /connect)", _MARKER)
        return
    try:
        _MARKER.parent.mkdir(parents=True, exist_ok=True)
        _MARKER.write_text("1")
    except OSError:
        pass  # best-effort; worst case the prompt repeats next boot
    threading.Thread(target=lambda: run(wait_for_tui=True), daemon=True, name="humalike-login").start()


if __name__ == "__main__":
    sys.exit(run())
