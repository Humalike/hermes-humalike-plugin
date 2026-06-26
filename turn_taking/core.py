"""Turn-taking logic — persona, the decide gate, naturalize, observed-context.

The platform-agnostic heart: given inbound events it decides speak/stay_silent
and stashes the epoch (decide), and given a finished draft it ships it for
naturalization (respond). No monkeypatching here — it talks to ``service`` (wire
calls), ``delivery`` (thread lifecycle), and ``state`` (the epoch/session maps).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from . import state
from .. import social_learning
from .delivery import _ensure_thread
from .service import _HERMES_CONFIG, _to_messages, respond, submit_messages

_log = logging.getLogger("hermes.plugins.turn_taking")


def _build_system_prompt_for_turn_taking(adapter: Any = None, session_id: Optional[str] = None) -> Optional[str]:
    """The bot's voice/style/personality, passed to the service so decide/
    naturalize/foresee speak in the bot's voice.

    Only the persona parts of Hermes' system prompt — never tool/skill schemas:
    ``SOUL.md`` (static personality) plus the social-learning voice card (the
    live per-session voice). Falls back to the gateway live value (runtime
    ``/personality`` change), ``HERMES_EPHEMERAL_SYSTEM_PROMPT``, or
    ``agent.system_prompt`` in ~/.hermes/config.yaml. None when unset (generic).
    """
    # Voice/style/personality only: SOUL.md + the live voice card. Both are small
    # persona text (no tool/skill schemas), so they stay under the service's
    # agent_instructions cap. ponytail: SOUL.md read straight off disk like
    # _HERMES_CONFIG; the card is read in-process from the social-learning plugin
    # (both plugins share the one gateway process) — coupled to its _CACHE name,
    # silently skipped if it moves.
    parts: list[str] = []
    try:
        soul = _HERMES_CONFIG.with_name("SOUL.md").read_text().strip()
        if soul:
            parts.append(soul)
    except Exception:
        pass
    if session_id:
        try:
            # Embedded social-learning module (same gateway process, same _CACHE
            # the pre_llm_call hook fills) — no cross-plugin import.
            card = social_learning._CACHE.get(session_id)
            if card:
                parts.append(card)
        except Exception:
            pass
    if parts:
        return "\n\n".join(parts)
    try:
        gw = getattr(getattr(adapter, "_message_handler", None), "__self__", None)
        live = getattr(gw, "_ephemeral_system_prompt", None)
        if live:
            return live
    except Exception:
        pass
    env = os.getenv("HERMES_EPHEMERAL_SYSTEM_PROMPT", "")
    if env:
        return env
    try:
        import yaml

        cfg = yaml.safe_load(_HERMES_CONFIG.read_text()) or {}
        return str((cfg.get("agent") or {}).get("system_prompt", "")).strip() or None
    except Exception:
        return None


# ── Decide gate: submit a batch, decide, stash the speak epoch ────────────────
async def _decide(
    session_id: str,
    adapter: Any,
    chat_id: str,
    messages: list[Dict[str, str]],
    system_prompt: Optional[str] = None,
    message_id: str = "",
) -> Optional[str]:
    """Submit a batch and return the decision ("speak" / "stay_silent").

    On "speak" the epoch is stashed in ``state.EPOCH_BY_MESSAGE_ID[message_id]`` so
    the later ``respond`` for THIS turn can carry its own epoch (fail-closed).
    Returns None when turn-taking is unavailable (no thread / service error) —
    caller then behaves as if turn-taking is off (let Hermes reply normally).
    """
    tid = await _ensure_thread(session_id, adapter, chat_id)
    if not tid:
        return None
    res = await submit_messages(tid, messages, system_prompt)
    if not res:
        return None
    decision = res.get("decision")
    epoch = res.get("turn_epoch")
    if decision == "speak" and message_id:
        # ponytail: no message_id → can't correlate at respond time, so don't stash;
        # the turn then degrades to a plain (un-naturalized) reply rather than risk a
        # mismatched epoch. WhatsApp always carries one, so this is the rare path.
        state.EPOCH_BY_MESSAGE_ID[message_id] = epoch
    _log.info("tt decide: session=%s chat=%s mid=%s decision=%s epoch=%s",
              session_id, chat_id, message_id, decision, epoch)
    return decision


# ── Respond side: naturalize a draft, carry this turn's epoch ──────────────────
async def _respond(
    session_id: str, draft: str, epoch: Optional[int], system_prompt: Optional[str] = None
) -> bool:
    """Naturalize a completed draft using THIS turn's epoch (resolved by the
    transform hook from the per-turn message_id) and call respond. In WS mode the
    bubbles are delivered by the receive loop, so nothing is returned for
    sending — the bool just says whether a reply was scheduled:

    - True  → scheduled; bubbles will arrive over WS.
    - False → never decided speak / superseded (newer batch won) / service error.
    """
    tid = state.SESSIONS.get(session_id)
    if not tid or epoch is None:
        _log.info("tt respond: session=%s → SKIP (no thread / no speak epoch)", session_id)
        return False  # this session never decided speak
    _log.info("tt respond: session=%s tid=%s epoch=%s → naturalizing | %r",
              session_id, tid, epoch, (draft or "").strip()[:60])
    res = await respond(tid, draft, epoch, system_prompt)
    if not res or res.get("superseded"):
        _log.info("tt respond: session=%s → DROPPED (superseded=%s / no response)",
                  session_id, bool(res and res.get("superseded")))
        return False  # dropped: a newer batch arrived, or service error
    _log.info("tt respond: session=%s → scheduled=%s (bubbles will arrive over WS)",
              session_id, res.get("scheduled"))
    return bool(res.get("scheduled"))


# ── Inbound gate: stay_silent → keep context, don't reply ─────────────────────
def _persist_observed(session_store: Any, session_id: str, events: list) -> None:
    """Append inbound messages to Hermes history WITHOUT dispatching the agent.

    Used on a stay_silent decision so a later "speak" turn still has the context
    the bot stayed quiet on. ``observed: True`` marks the rows as context (they
    replay as background, not as unanswered user turns). The ``[Name]`` prefix is
    the Hermes-side authorship convention, so the agent knows who said what.
    """
    for ev in events:
        text = (getattr(ev, "text", "") or "").strip()
        if not text:
            continue
        name = getattr(getattr(ev, "source", None), "user_name", None)
        entry = {"role": "user", "content": f"[{name}] {text}" if name else text, "observed": True}
        mid = getattr(ev, "message_id", None)
        if mid:
            entry["message_id"] = str(mid)
        try:
            session_store.append_to_transcript(session_id, entry)
        except Exception as e:
            _log.warning("turn-taking: persist observed failed: %s", e)


async def _inbound_gate(
    adapter: Any,
    session_store: Any,
    session_id: str,
    chat_id: str,
    events: list,
    system_prompt: Optional[str] = None,
    message_id: str = "",
) -> bool:
    """Decide whether an inbound batch should reach the agent.

    - "speak" or service unavailable (None) → return True: dispatch normally, and
      Hermes persists the turn itself.
    - "stay_silent" → persist the messages as observed context and return False:
      the bot keeps quiet but remembers.
    """
    messages = _to_messages(events)
    if not messages:
        return True  # nothing to decide → let Hermes handle it
    decision = await _decide(session_id, adapter, chat_id, messages, system_prompt, message_id)
    if decision == "stay_silent":
        _persist_observed(session_store, session_id, events)
        _log.info("tt gate: session=%s chat=%s → STAY_SILENT (persisted %d observed msg(s), no dispatch)",
                  session_id, chat_id, len(messages))
        return False
    _log.info("tt gate: session=%s chat=%s → PROCEED (decision=%s) → dispatch agent",
              session_id, chat_id, decision)
    return True  # speak, or None (fail-open: behave as if turn-taking is off)
