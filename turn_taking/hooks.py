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


def _queued_follow_up(chat: str) -> bool:
    """True when the gateway holds a QUEUED follow-up event for this chat.

    ``state.PENDING_MAP`` is the live ``pending_messages`` dict (captured by the
    merge wrapper); its keys are session keys that embed the chat id. No map yet
    (nothing was ever queued) → no follow-up. Fail-open to False: worst case we
    stamp the newest epoch and the service arbitrates via its one-shot claim.
    """
    if not chat or not state.PENDING_MAP:
        return False
    try:
        # Session keys embed the chat id as a ':'-separated segment
        # (gateway/session.py build_session_key: "{ns}:{platform}:dm:{chat_id}",
        # group/thread variants likewise). Exact-segment match — a substring
        # test would false-positive on numeric ids ("123" ⊂ "…:1234") and skip
        # the re-stamp, silently reintroducing the dropped-reply bug.
        return any(chat in str(k).split(":") for k in state.PENDING_MAP)
    except Exception:
        return False


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
    # A follow-up that arrived mid-turn gets merged into THIS turn's context, but
    # the epoch above was bound at turn start — the service would drop the reply
    # as superseded even though it answers the newest message. Stamp with the
    # chat's latest epoch instead, unless a QUEUED follow-up turn exists (then
    # the newest epoch belongs to its answer, not ours).
    # ponytail: this is decidedly hacky — we infer "merged vs queued" by peeking
    # at a live ref to the gateway's private pending_messages dict (PENDING_MAP)
    # and correct the epoch after the fact, instead of the turn knowing what it
    # actually consumed. Works, but rests on host internals. Worth a refactor
    # someday: either the service accepts respond(claim_latest=true) and
    # arbitrates atomically, or Hermes exposes "messages consumed by this turn"
    # so the anchor/epoch can be bound correctly at the source.
    latest = state.LATEST_EPOCH_BY_CHAT.get(chat or "")
    if latest is not None and latest != epoch and not _queued_follow_up(chat):
        _log.info("tt transform: epoch %s → %s (follow-up merged into this turn)", epoch, latest)
        epoch = latest
    if chat:
        _suppress_answer(chat, draft)  # drop the send whose content matches this answer
    _log.info("tt transform: session=%s chat=%s mid=%s epoch=%s → naturalize + suppress raw send | %r",
              sid, chat, mid, epoch, (draft or "").strip()[:50])
    # transform_llm_output runs in the agent's worker thread (no running loop here),
    # so hand _respond to the gateway loop captured on inbound. A bare create_task
    # raises "no running event loop" and the naturalize call is silently lost.
    _coro = _respond(sid, draft, epoch, _build_system_prompt_for_turn_taking(None, sid), meta)
    try:
        if state.LOOP is not None:
            asyncio.run_coroutine_threadsafe(_coro, state.LOOP)  # bubbles delivered via WS
        else:
            asyncio.create_task(_coro)  # fallback: already in a loop
    except Exception as e:
        _coro.close()
        _log.warning("tt transform: could not schedule _respond: %s", e)
    return None
