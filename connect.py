"""`/connect` — link this agent to a Humalike account (device authorization).

The chat-first cousin of ``hermes setup``: the user sends ``/connect`` from any
platform, gets back a login link, opens it in a browser on ANY device (their
phone works — the gateway box never needs a display, so SSH/VM/Docker installs
are first-class), approves, and the freshly minted API key is saved. The key
goes live in-process immediately (``os.environ`` — every Humalike call reads it
fresh via ``_config.api_key()``) and into ``~/.hermes/.env`` for the next boot,
so no restart is needed.

Server side is Humalike's RFC 8628 lane on the keys service:
  POST {base}/v1/keys/actions/cli_create  -> {device_code, user_code,
                                              verification_uri, expires_in, interval}
  POST {base}/v1/keys/actions/cli_poll    {device_code} -> {status, api_key?, account?}

``status`` is pending/authorized/denied/expired; on ``authorized`` the response
carries the ``ak_…`` key exactly once (the server deletes the session as the
claim). Any HTTP error while polling is transient by contract — keep polling;
the session TTL, not this loop, is the real deadline.

Command handlers only get ``raw_args`` and return one reply string, so the
approval outcome is delivered later via the route captured at command time
(``state.LAST_ADAPTER``/``LAST_CHAT_ID``, set by the inbound gate for the
/connect message itself) — sent through the genuine pre-patch ``send`` so the
draft-suppression patch can't swallow it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import socket
import threading
import time
from pathlib import Path
from typing import Any, Optional, Tuple

import httpx

from . import _config
from .turn_taking import notify, state

_log = logging.getLogger(__name__)

_ENV_FILE = Path.home() / ".hermes" / ".env"
_DEFAULT_API = "https://api.humalike.com"
_CREATE = "/v1/keys/actions/cli_create"
_POLL = "/v1/keys/actions/cli_poll"

# RFC 8628 public client identifier for the CLI lane: it names the client (a
# Hermes plugin install) to the API and unlocks ONLY cli_create/cli_poll — the
# per-session device_code stays the sole credential that can claim a key, so
# this ships in the open like any OAuth public client_id.
# ponytail: no baked default yet — set HUMALIKE_CLI_GATEWAY_KEY until the
# published identifier lands here in a follow-up.
_GATEWAY_KEY_DEFAULT = ""

_PENDING = threading.Event()  # one in-flight link at a time (cleared by _watch)


def _api_url() -> str:
    """Connect must work BEFORE install finishes, so unlike the turn-taking
    service it falls back to the public API when HUMALIKE_API_URL is unset."""
    return _config.service_url() or _DEFAULT_API


def _gateway_key() -> str:
    return os.getenv("HUMALIKE_CLI_GATEWAY_KEY") or _GATEWAY_KEY_DEFAULT


def _headers() -> dict:
    return {"Authorization": f"Bearer {_gateway_key()}", "Content-Type": "application/json"}


# ── Command handler (registered as /connect) ──────────────────────────────────
async def command(raw_args: str) -> str:
    """Start a device-auth session and reply with the approval link."""
    # Capture the invoking chat FIRST, before any await: the /connect message
    # itself just went through the inbound gate, so the pair still names this
    # chat. Awaiting first would let any other chat's inbound overwrite the
    # globals during the HTTP round-trip and mis-route the confirmation. A
    # residual race (another chat between the gate and this handler) remains;
    # it carries no secret — worst case the wrong chat sees "Connected as <email>".
    route = (state.LAST_ADAPTER, state.LAST_CHAT_ID)
    if _config.api_key():
        return ("✅ Already connected — HUMALIKE_API_KEY is set. To relink, remove it from "
                f"{_ENV_FILE}, restart the gateway, and send /connect again.")
    if not _gateway_key():
        return ("⚠️ /connect isn't configured on this install (no HUMALIKE_CLI_GATEWAY_KEY). "
                "Create an API key at https://humalike.com and add it to "
                f"{_ENV_FILE} as HUMALIKE_API_KEY=… instead.")
    if _PENDING.is_set():
        return "⏳ A connect link is already waiting to be approved — open it, or wait for it to expire and send /connect again."
    # No await between the is_set() check and set() (single-threaded loop), so
    # two concurrent /connect can't both pass. Cleared by _watch, or below on failure.
    _PENDING.set()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                _api_url() + _CREATE,
                json={"client": "hermes", "hostname": socket.gethostname(), "os": platform.system()},
                headers=_headers(),
            )
            r.raise_for_status()
            session = r.json()
        threading.Thread(target=_watch, args=(route, session), daemon=True, name="humalike-connect").start()
    except Exception as e:
        _PENDING.clear()
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status is not None:
            _log.warning("connect: cli_create → HTTP %s: %s", status, getattr(e.response, "text", "")[:200])
            return (f"⚠️ Humalike rejected the connect request (HTTP {status}) — "
                    "check HUMALIKE_CLI_GATEWAY_KEY, then send /connect again.")
        _log.warning("connect: cli_create failed: %s", e)
        return "⚠️ Couldn't reach Humalike to start the link — check the gateway's network and send /connect again."

    minutes = max(1, int(session.get("expires_in", 600)) // 60)
    if route[0] is not None and route[1]:
        tail = f"I'll confirm here once you're done — the link is valid for ~{minutes} minutes."
    else:
        # No deliverable route (URL unset → no patches, or a platform whose
        # commands bypass our inbound gate): don't promise a confirmation we
        # can't send — the outcome lands in the gateway log instead.
        tail = (f"The link is valid for ~{minutes} minutes. I can't post a confirmation in this chat, "
                f"so after approving, check that HUMALIKE_API_KEY appeared in {_ENV_FILE} "
                "(the outcome is also in the gateway log).")
    reply = ("🔗 Open this link on any device and approve to connect your Humalike account:\n\n"
             f"{session['verification_uri']}\n\n"
             f"{tail} "
             "(Anyone who opens it can link their own account, so prefer a DM in a busy group.)")
    if not _config.service_url():
        reply += ("\n\nHeads-up: HUMALIKE_API_URL isn't set, so after approving you still need to add "
                  f"HUMALIKE_API_URL={_DEFAULT_API} to {_ENV_FILE} and restart the gateway.")
    return reply


# ── Background poll ────────────────────────────────────────────────────────────
def _watch(route: Tuple[Any, str], session: dict) -> None:
    """Poll until approved/denied/expired, then confirm in the /connect chat.

    Own thread + own loop (soul's auto-enhance idiom) so neither the command
    reply nor the gateway loop ever blocks on the ~10-minute approval window.
    """
    try:
        message = asyncio.run(_poll(session))
    except Exception as e:  # never let the watcher die silently
        _log.warning("connect: poll loop errored: %s", e)
        message = None
    finally:
        _PENDING.clear()
    if message:
        _log.info("connect: %s", message)  # headless installs read the outcome here
        _deliver(route, message)


async def _poll(session: dict) -> Optional[str]:
    interval = max(1, int(session.get("interval", 3)))
    deadline = time.monotonic() + int(session.get("expires_in", 600))
    async with httpx.AsyncClient(timeout=15.0) as client:
        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            try:
                r = await client.post(
                    _api_url() + _POLL,
                    json={"device_code": session["device_code"]},
                    headers=_headers(),
                )
                r.raise_for_status()
                data = r.json()
            except Exception:
                continue  # transient by contract — the session TTL is the deadline
            status = data.get("status")
            if status == "authorized":
                _save_key(data.get("api_key") or "")
                return _done_message(data)
            if status in ("denied", "expired"):
                return _result_message(status)
            # pending (or an unknown future status) → keep polling until TTL
    return _result_message("expired")


def _result_message(status: str) -> str:
    if status == "denied":
        return "❌ The connect request was denied — send /connect to try again."
    return "⌛ The connect link expired before it was approved — send /connect for a fresh one."


def _done_message(data: dict) -> str:
    who = (data.get("account") or {}).get("email") or "your account"
    if _config.service_url():
        return f"✅ Connected as {who} — key saved, turn-taking is active."
    return (f"✅ Connected as {who} — key saved to {_ENV_FILE}. Now set HUMALIKE_API_URL there "
            "and restart the gateway to activate turn-taking.")


# ── Key persistence ────────────────────────────────────────────────────────────
def _save_key(key: str) -> None:
    """Live for THIS gateway (env — read fresh on every Humalike call, so
    turn-taking activates with no restart) and durable for the next one."""
    os.environ["HUMALIKE_API_KEY"] = key
    try:
        _write_env(_ENV_FILE, key)
    except Exception as e:
        _log.warning("connect: could not write %s (%s) — key active until restart only", _ENV_FILE, e)


def _write_env(path: Path, key: str) -> None:
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


# ── Outcome delivery ───────────────────────────────────────────────────────────
def _deliver(route: Tuple[Any, str], text: str) -> None:
    """Send the outcome to the chat /connect came from, via the genuine
    pre-patch ``send`` (notify's idiom) so the draft-suppression patch can't
    swallow it. No route (e.g. a platform whose commands bypass our inbound
    gate) → the home-channel startup queue as a best-effort fallback — that
    queue only flushes while the patches are active, which is why command()
    stops promising an in-chat confirmation when the route is empty.
    ponytail: sent without platform metadata, so on a Telegram forum-group the
    confirmation lands in the General topic, not the invoking one."""
    adapter, chat_id = route
    if adapter is None or not chat_id:
        notify.queue_startup(text)
        return

    async def _send() -> None:
        try:
            orig = state.ORIG_SEND.get(type(adapter))
            if orig is not None:
                await orig(adapter, str(chat_id), text)
            else:
                await adapter.send(str(chat_id), text)
        except Exception as e:
            _log.warning("connect: confirmation send failed: %s", e)

    notify._schedule(_send)
