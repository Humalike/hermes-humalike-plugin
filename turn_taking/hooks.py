"""Hermes plugin hooks — the ``transform_llm_output`` handler.

A hook is not a monkeypatch: Hermes calls these by name (registered in
``register()``), so they live apart from ``patching``. This one fires once per
turn with the agent's final answer, ships it for naturalization, and flags its
raw send for suppression. Reads the per-turn message_id that ``patching``'s
``_run_agent`` wrapper stashed in ``state`` (write side there, read side here).
"""

from __future__ import annotations

import asyncio
import logging

from . import state
from .core import _build_system_prompt_for_turn_taking, _respond
from .delivery import _chat_for_session

_log = logging.getLogger(__name__)


def _suppress_answer(chat_id: str, content: str) -> None:
    state.PENDING_ANSWERS.setdefault(chat_id, set()).add((content or "").strip())


def _current_message_id() -> str:
    """The inbound platform message_id of the turn currently being answered.

    Read from our own per-task contextvar (``_TT_MID_CTX``), bound per turn as a
    side effect of ``GatewayRunner._reply_anchor_for_event`` (see
    _patch__reply_anchor_for_event) and propagated into the worker thread via
    ``copy_context()``. Task-local, so concurrent turns never
    see each other's id, and queued follow-up turns get their OWN id. "" when
    unavailable (then we leave Hermes alone). (Hermes's own
    ``HERMES_SESSION_MESSAGE_ID`` is empty on the WhatsApp path — hence our var.)
    """
    return state.TT_MID_CTX.get("") or ""


# Label for the recalled memory wherever it is injected — the reply draft (the
# pre_llm_call hook, into Hermes's user message) and the respond system_prompt.
_MEMORY_LABEL = "What you know about the people here (from memory):"


def on_pre_llm_call(**kwargs):
    """pre_llm_call hook: inject the social-memory context recalled at decide into
    the reply draft, so what the agent knows about these people shapes WHAT it
    writes. Mirrors the social-learning voice card. Returns None (nothing
    injected) when the session has no memory — memory off, empty, or an API that
    does not return ``recalled_context``.
    """
    session_id = kwargs.get("session_id") or ""
    mem = state.MEMORY_BY_SESSION.get(session_id) if session_id else ""
    if not mem:
        return None
    return {"context": f"{_MEMORY_LABEL}\n{mem}"}


def _with_memory(system_prompt, session_id):
    """Fold the turn's recalled memory into the respond system_prompt, so the
    reply-refinement steps see it too — the same single recall, no second lookup.
    Unchanged when there is no memory."""
    mem = state.MEMORY_BY_SESSION.get(session_id) if session_id else ""
    if not mem:
        return system_prompt
    block = f"{_MEMORY_LABEL}\n{mem}"
    return f"{system_prompt}\n\n{block}" if system_prompt else block


def on_transform_llm_output(response_text=None, session_id=None, **kwargs):
    """Fired once per turn with the agent's FINAL answer (after the tool loop).

    Tool-progress sends (💻/⚠️/✅) do NOT pass through here, so this naturalizes
    the *answer* precisely — unlike the send patch, which can't tell the answer
    from tool noise. Fires _respond (bubbles over WS) and flags the imminent raw
    send of this answer for suppression. Returns None so the draft stays in
    Hermes history (only its *send* is dropped).
    """
    draft = response_text
    sid = session_id or ""
    # Recover THIS turn's epoch by the message_id it answers (read from the
    # per-task contextvar, looked up in the map filled at decide). consume once.
    mid = _current_message_id()
    epoch = state.EPOCH_BY_MESSAGE_ID.pop(mid, None) if mid else None
    meta = state.META_BY_MESSAGE_ID.pop(mid, None) if mid else None
    if not draft or epoch is None:
        return None  # not a turn-taking speak turn → leave Hermes alone
    chat = _chat_for_session(sid)
    if chat:
        _suppress_answer(chat, draft)  # drop the send whose content matches this answer
    _log.info("tt transform: session=%s chat=%s mid=%s epoch=%s → naturalize + suppress raw send | %r",
              sid, chat, mid, epoch, (draft or "").strip()[:50])
    # transform_llm_output runs in the agent's worker thread (no running loop here),
    # so hand _respond to the gateway loop captured on inbound. A bare create_task
    # raises "no running event loop" and the naturalize call is silently lost.
    _coro = _respond(sid, draft, epoch, _with_memory(_build_system_prompt_for_turn_taking(None, sid), sid), meta)
    try:
        if state.LOOP is not None:
            asyncio.run_coroutine_threadsafe(_coro, state.LOOP)  # bubbles delivered via WS
        else:
            asyncio.create_task(_coro)  # fallback: already in a loop
    except Exception as e:
        _coro.close()
        _log.warning("tt transform: could not schedule _respond: %s", e)
    return None
