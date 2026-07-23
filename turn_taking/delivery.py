"""Bubble delivery + thread/session lifecycle.

The outbound half of the loop: open a turn-taking thread per conversation, route
its naturalized bubbles back to the right chat, and run the WS receive loop. Talks
to ``service`` for the wire calls and ``state`` for the route/session maps; knows
nothing about the monkeypatches.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from . import state
from .service import _connect_url, _receive_loop, _thread_id, open_thread

_log = logging.getLogger(__name__)


def _set_route(thread_id: str, adapter: Any, chat_id: str) -> None:
    """Remember where a thread's delivered bubbles should be sent."""
    state.ROUTES[thread_id] = (adapter, chat_id)


async def _forward(
    thread_id: Optional[str], content: Optional[str], metadata: Optional[dict] = None
) -> None:
    """on_message callback: route one delivered bubble to its chat.

    ``metadata`` is the service's verbatim echo of the respond's metadata — here the
    forum-topic id ({"thread_id": ...}), passed straight to ``send`` so the bubble
    lands in the source topic (Telegram reads ``metadata["thread_id"]``; WhatsApp
    ignores it). None/empty → normal placement.

    No route (e.g. after a restart lost the map) → log + drop, never crash the
    receive loop.
    """
    if not content:
        return
    route = state.ROUTES.get(thread_id or "")
    if route is None:
        _log.warning("turn-taking: no route for thread %s — dropping bubble", thread_id)
        return
    adapter, chat_id = route
    orig = state.ORIG_SEND.get(type(adapter))
    _log.info("tt forward: tid=%s chat=%s → deliver bubble (via=%s, topic=%s) | %r",
              thread_id, chat_id, "orig" if orig is not None else "plain",
              (metadata or {}).get("thread_id"), content[:60])
    # Deliver via this adapter class's original send so the bubble bypasses our
    # patch (which drops the agent's draft). Before _patch_send runs, adapter.send
    # IS the original. Pass metadata so a forum-topic bubble lands in its topic.
    if orig is not None:
        await orig(adapter, chat_id, content, metadata=metadata or None)
    else:
        await adapter.send(chat_id, content, metadata=metadata or None)


async def _forward_typing(thread_id: Optional[str], is_typing: Optional[bool]) -> None:
    """on_typing callback: show "… is typing" while the reply is paced.

    WhatsApp/Telegram presence auto-expires after a few seconds, so typing-start
    alone was enough there. Discord's ``send_typing`` instead starts a persistent
    refresh loop that runs until ``stop_typing`` — without an explicit stop the
    indicator loops forever after the bubbles land. So: start on typing-start,
    and on typing-stop call the adapter's ``stop_typing`` when it has one
    (harmless no-op elsewhere).
    """
    route = state.ROUTES.get(thread_id or "")
    if route is None:
        return
    adapter, chat_id = route
    try:
        if is_typing:
            # On adapters whose host typing is muted (Discord), call the genuine
            # send_typing — the patched one is a no-op. Walk the MRO: the mute is
            # registered under the resolved base class, but the live adapter may
            # be a subclass (plain type() lookup would miss and hit the no-op).
            orig = next(
                (state.ORIG_SEND_TYPING[c] for c in type(adapter).__mro__
                 if c in state.ORIG_SEND_TYPING),
                None,
            )
            if orig is not None:
                await orig(adapter, chat_id)
            else:
                await adapter.send_typing(chat_id)
        else:
            stop = getattr(adapter, "stop_typing", None)
            if stop is not None:
                await stop(chat_id)
    except Exception as e:
        _log.warning("turn-taking typing failed: %s", e)


# ── Delivery bootstrap: open thread + route + start the WS loop ────────────────
async def _start_delivery(
    adapter: Any, chat_id: str, thread_id: Optional[str] = None
) -> Optional[str]:
    """Open/reopen a thread, register its route, and start its WS receive loop.

    Returns the thread_id (use it for submit_messages / respond), or None if
    open_thread failed (fail-open: caller behaves as if turn-taking is off).

    ponytail: spawn-and-forget — assumes a stable connection, so no task
    tracking / reconnect yet. Caller dedupes (one start per conversation); the
    receive loop connects in ~ms, well before bubbles come due (~reading delay).
    """
    resp = await open_thread(thread_id)
    tid = _thread_id(resp)
    url = _connect_url(resp)
    if not tid or not url:
        _log.warning("turn-taking: open_thread failed — no delivery for chat %s", chat_id)
        return None
    _set_route(tid, adapter, chat_id)
    _log.info("tt delivery: thread opened tid=%s for chat=%s → starting WS loop", tid, chat_id)
    asyncio.create_task(_receive_loop(url, _forward, _forward_typing))
    return tid


async def _ensure_thread(session_id: str, adapter: Any, chat_id: str) -> Optional[str]:
    """Get-or-create the turn-taking thread for a Hermes session.

    First activity per session opens the thread + WS delivery; later activity
    reuses it. Returns thread_id, or None if open_thread failed.

    Since messages are gated per-message (no batch), two first-messages of a new
    session can race; the lock + re-check makes the open once-only.
    """
    tid = state.SESSIONS.get(session_id)
    if tid:
        return tid
    async with state.OPEN_LOCK:
        tid = state.SESSIONS.get(session_id)  # re-check under lock
        if tid:
            return tid
        tid = await _start_delivery(adapter, chat_id)
        if tid:
            state.SESSIONS[session_id] = tid
        return tid


def _chat_for_session(session_id: str) -> Optional[str]:
    """The WhatsApp chat_id a session delivers to (session → thread → route)."""
    thread = state.SESSIONS.get(session_id)
    route = state.ROUTES.get(thread or "") if thread else None
    return route[1] if route else None
