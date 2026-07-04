"""Home-chat alerts when the Humalike API fails; never raises, never blocks.

Delivered to EVERY ``/sethome`` home channel (like the gateway's own startup/
shutdown broadcasts). Rate-limited PER error-kind so a dead service can't spam
and a second, different failure (e.g. a WS drop after an HTTP outage) isn't
masked by the first. Safe to call from any thread — off-loop callers (the
social-learning daemon) are scheduled onto the captured gateway loop.

Kinds: ``auth`` (bad key), ``quota`` (credit/limit), ``unreachable`` (no
connection), ``server`` (5xx), ``ws`` (realtime drop), ``config`` (startup
misconfig). A successful call clears the active kinds and posts one recovery
line.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Optional

from . import state

_log = logging.getLogger(__name__)

_COOLDOWN_S = 30 * 60
_LOCK = threading.Lock()             # alert() is now called from >1 thread
_last_by_kind: dict[str, float] = {}  # error kind → monotonic ts of last alert
_active_kinds: set[str] = set()       # kinds currently in the "failing" state
_pending: list = []                   # (text, on_delivered) startup msgs, flushed on 1st inbound

WS_LOST = ("⚠️ Humalike realtime connection lost — the bot may go SILENT in "
           "affected chats until the gateway restarts.")


def _kind(status: Optional[int]) -> str:
    if status in (401, 403):
        return "auth"
    if status in (402, 429):
        return "quota"
    if status is None:
        return "unreachable"
    return "server"


def _why(status: Optional[int]) -> str:
    if status in (401, 403):
        return "API key rejected — check HUMALIKE_API_KEY"
    if status in (402, 429):
        return "credit/quota exhausted — top up to restore turn-taking"
    if status is None:
        return "service unreachable — check HUMALIKE_API_URL"
    return f"service failing (HTTP {status})"


# ── Scheduling (loop-safe) ────────────────────────────────────────────────────
def _schedule(make_coro) -> None:
    """Run ``make_coro()`` on the gateway loop, whether we're on it or not.

    ``make_coro`` is a thunk so the coroutine is only created once a loop
    exists (no "coroutine was never awaited" warning when there's none).
    """
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    loop = running or state.LOOP  # off-loop caller → the captured gateway loop
    if loop is None:
        return
    if running is loop:
        loop.create_task(make_coro())
    else:
        loop.call_soon_threadsafe(lambda: loop.create_task(make_coro()))


def _fire(kind: str, text: str) -> None:
    """Rate-limit by kind, mark it active, and schedule the send."""
    with _LOCK:
        now = time.monotonic()
        if now - _last_by_kind.get(kind, -_COOLDOWN_S) < _COOLDOWN_S:
            return
        _last_by_kind[kind] = now
        _active_kinds.add(kind)
    _schedule(lambda: _send(text))


# ── Public API ────────────────────────────────────────────────────────────────
def alert(e: Optional[Exception] = None, text: Optional[str] = None,
          kind: Optional[str] = None, status: Optional[int] = None) -> None:
    """Fire-and-forget a rate-limited home-channel alert for a failed API call."""
    try:
        if status is None:
            status = getattr(getattr(e, "response", None), "status_code", None)
        k = kind or _kind(status)
        msg = text or f"⚠️ Humalike {_why(status)} — the bot replies unfiltered until this is fixed."
        _fire(k, msg)
    except Exception:
        pass  # alerting must never hurt message flow (already logged by caller)


def alert_social(e: Optional[Exception] = None, status: Optional[int] = None) -> None:
    """Alert for a social-learning refresh failure (shares kinds with turn-taking
    so a full outage deduplicates to one message)."""
    try:
        if status is None:
            status = getattr(getattr(e, "response", None), "status_code", None)
        _fire(_kind(status),
              f"⚠️ Humalike {_why(status)} — voice-matching (social-learning) paused.")
    except Exception:
        pass


def recovered() -> None:
    """Call on a successful API response: if we'd alerted, post recovery once."""
    try:
        with _LOCK:
            if not _active_kinds:
                return
            _active_kinds.clear()
            _last_by_kind.clear()  # let the next failure alert immediately
        _schedule(lambda: _send("✅ Humalike recovered — turn-taking active again."))
    except Exception:
        pass


def queue_startup(text: str, on_delivered=None) -> None:
    """Queue a startup misconfig alert; delivered on the first inbound message
    (adapters aren't connected yet at register() time). ``on_delivered`` fires
    only after the text actually reached at least one home channel — callers
    use it to record "warned" without losing warnings that never got through."""
    with _LOCK:
        _pending.append((text, on_delivered))


def flush_pending() -> None:
    """Deliver queued startup alerts once an adapter is available. Cheap no-op
    after the first flush — safe to call on every inbound."""
    if not _pending:
        return
    with _LOCK:
        msgs, _pending[:] = list(_pending), []
    for m, cb in msgs:
        _schedule(lambda m=m, cb=cb: _deliver_startup(m, cb))


async def _deliver_startup(text: str, cb) -> None:
    if not await _send(text):
        # Not delivered (no gateway/home channel yet, or the send failed) —
        # re-queue so a later inbound retries, e.g. once /sethome is run.
        with _LOCK:
            _pending.append((text, cb))
        return
    if cb is not None:
        try:
            cb()
        except Exception:
            _log.warning("notify: on_delivered callback failed", exc_info=True)


# ── Delivery ──────────────────────────────────────────────────────────────────
def _gateway() -> Any:
    """Reach the gateway from any adapter we've seen (ROUTES, else the last
    inbound adapter — covers 'service dead from startup, no thread opened')."""
    adapters = [a for a, _chat in state.ROUTES.values()]
    if state.LAST_ADAPTER is not None:
        adapters.append(state.LAST_ADAPTER)
    for adapter in adapters:
        gw = getattr(getattr(adapter, "_message_handler", None), "__self__", None)
        if gw is not None:
            return gw
    return None


async def _send(text: str) -> bool:
    gw = _gateway()
    if gw is None:
        return False  # no adapter/gateway seen yet — nothing to send through (caller logged)
    # Every configured home channel — one platform failing must not skip the rest.
    sent_any = False
    for platform, adp in list(getattr(gw, "adapters", {}).items()):
        try:
            home = gw.config.get_home_channel(platform)
            if not home or not home.chat_id:
                continue
            # Bypass our own send patch — it would route the alert through
            # turn-taking and drop it as an unsolicited draft.
            orig = state.ORIG_SEND.get(type(adp))
            if orig:
                res = await orig(adp, str(home.chat_id), text)
            else:
                res = await adp.send(str(home.chat_id), text)
            # Host adapters don't raise on delivery failure — they return
            # SendResult(success=False) ("Not connected", flood control…), so
            # "no exception" is NOT "sent". Default True covers adapters that
            # return nothing.
            if getattr(res, "success", True):
                sent_any = True
            else:
                _log.warning("notify: alert to %s home not delivered: %s",
                             platform, getattr(res, "error", "send failed"))
        except Exception as e:
            _log.warning("notify: alert to %s home failed: %s", platform, e)
    if not sent_any:
        _log.info("notify: no home channel set — run /sethome to get alerts in chat")
    return sent_any
