"""Hermes turn-taking plugin — svc-turn-taking integration (WebSocket delivery).

Built in tiny chunks.
  CHUNK 1: config + auth.
  CHUNK 2: transport — _post + action paths. Still no high-level actions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

import httpx

_log = logging.getLogger("hermes.plugins.turn_taking")

# ── Wire contract (svc-turn-taking action paths) ──────────────────────────────
OPEN_THREAD_PATH = "/v1/turn-taking/actions/open_thread"
SUBMIT_PATH = "/v1/turn-taking/actions/submit_messages"
RESPOND_PATH = "/v1/turn-taking/actions/respond"

_HERMES_CONFIG = Path.home() / ".hermes" / "config.yaml"


# ── Config + auth (chunk 1) ───────────────────────────────────────────────────
def _service_url() -> str:
    """Base URL. Env ``TURN_TAKING_SERVICE_URL`` wins; else config.yaml; "" if unset."""
    url = os.getenv("TURN_TAKING_SERVICE_URL", "")
    if not url:
        try:
            import yaml

            cfg = yaml.safe_load(_HERMES_CONFIG.read_text()) or {}
            url = str((cfg.get("turn_taking") or {}).get("service_url", ""))
        except Exception:
            url = ""
    return url.rstrip("/")


def _api_key() -> str:
    """Clerk API key (``ak_...``) from ``TURN_TAKING_API_KEY``."""
    return os.getenv("TURN_TAKING_API_KEY", "")


def _headers() -> Dict[str, str]:
    """Auth + content-type headers for every service call."""
    return {"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"}


def _persona(adapter: Any = None, session_id: Optional[str] = None) -> Optional[str]:
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
            from hermes_plugins.social_learning import _CACHE  # noqa: PLC0415

            card = _CACHE.get(session_id)
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


# ── Transport (chunk 2) ───────────────────────────────────────────────────────
async def _post(path: str, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """POST ``body`` to ``path`` and return parsed JSON, or None on any failure.

    Fail-open by design: an unconfigured URL or any network/HTTP error returns
    None so a service blip never wedges the gateway — callers fall back to
    "behave as if turn-taking is off".
    """
    base = _service_url()
    if not base:
        return None
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(base + path, json=body, headers=_headers())
            r.raise_for_status()
            return r.json()
    except httpx.HTTPStatusError as e:
        # 4xx = our request is wrong (bad key/payload) — actionable, log loud.
        # 5xx = service broken. Either way fail-open, but never silent.
        _log.warning(
            "turn-taking %s → HTTP %s: %s", path, e.response.status_code, e.response.text[:200]
        )
        return None
    except httpx.HTTPError as e:
        # connect refused / DNS / timeout — service unreachable, fail-open.
        _log.warning("turn-taking %s unreachable: %s", path, e)
        return None
    # ponytail: a non-JSON 2xx (r.json() → JSONDecodeError) and our own bugs
    # (KeyError/TypeError) now propagate instead of hiding as fail-open.


# ── Actions (chunk 3) ─────────────────────────────────────────────────────────
async def open_thread(thread_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Open/reopen a thread (id = idempotency key). Returns {thread, channel, realtime}."""
    return await _post(OPEN_THREAD_PATH, {"thread_id": thread_id} if thread_id else {})


async def submit_messages(
    thread_id: str,
    messages: list[Dict[str, str]],
    system_prompt: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Decide speak/stay_silent for a batch of {sender, content}. Returns {decision, turn_epoch}."""
    body: Dict[str, Any] = {"thread_id": thread_id, "messages": messages}
    if system_prompt:
        body["system_prompt"] = system_prompt
    return await _post(SUBMIT_PATH, body)


async def respond(
    thread_id: str,
    content: str,
    turn_epoch: int,
    system_prompt: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Naturalize a draft (epoch required, fail-closed). Returns {scheduled, superseded}."""
    body: Dict[str, Any] = {"thread_id": thread_id, "content": content, "turn_epoch": turn_epoch}
    if system_prompt:
        body["system_prompt"] = system_prompt
    return await _post(RESPOND_PATH, body)


# ── WS lifecycle (chunk 4: parse the open_thread grant) ───────────────────────
def _thread_id(open_resp: Optional[Dict[str, Any]]) -> Optional[str]:
    """The thread id from an open_thread response (None if malformed)."""
    return ((open_resp or {}).get("thread") or {}).get("id")


def _connect_url(open_resp: Optional[Dict[str, Any]]) -> Optional[str]:
    """The short-lived WebSocket connect URL (token-bound to one channel).

    ~30s TTL, so connect promptly after open_thread.
    """
    return ((open_resp or {}).get("realtime") or {}).get("connect_url")


async def _receive_loop(
    connect_url: str,
    on_message: Callable[[Optional[str], Optional[str]], Awaitable[None]],
    on_typing: Optional[Callable[[Optional[str], Optional[bool]], Awaitable[None]]] = None,
) -> None:
    """Read envelopes until the socket closes; dispatch each by ``type``.

    - ``turn_taking.message`` → ``await on_message(thread_id, content)`` (one bubble)
    - ``turn_taking.typing``  → ``await on_typing(thread_id, typing)`` (if provided)
    - ``attached`` (handshake) → ignored

    No reconnect: assumes a stable connection (D2). On close/error it logs and
    returns; the supervising task decides what to do next.
    """
    try:
        import websockets
    except Exception as e:  # dependency missing
        _log.warning("turn-taking WS unavailable (no websockets lib): %s", e)
        return
    try:
        async with websockets.connect(connect_url) as ws:
            _log.info("tt ws: connected | %s", connect_url[:80])
            async for frame in ws:
                try:
                    env = json.loads(frame)
                except Exception:
                    continue
                t = env.get("type")
                data = env.get("data") or {}
                _log.info("tt ws: frame type=%s tid=%s", t, data.get("thread_id"))
                if t == "turn_taking.message":
                    await on_message(data.get("thread_id"), data.get("content"))
                elif t == "turn_taking.typing" and on_typing is not None:
                    await on_typing(data.get("thread_id"), data.get("typing"))
    except Exception as e:
        _log.warning("turn-taking WS loop ended: %s", e)


# ── Delivery routing (chunk 7: reverse map + forwarder) ───────────────────────
# thread_id → (adapter, chat_id): where to deliver this thread's bubbles.
_ROUTES: Dict[str, Tuple[Any, str]] = {}

# The genuine WhatsAppAdapter.send, captured before patching (chunk 13/14), so
# _forward delivers bubbles via the ORIGINAL — they bypass our patch entirely,
# which is what suppresses the agent's monolithic draft. No metadata marker needed.
_ORIG_SEND: Optional[Callable] = None


def _set_route(thread_id: str, adapter: Any, chat_id: str) -> None:
    """Remember where a thread's delivered bubbles should be sent."""
    _ROUTES[thread_id] = (adapter, chat_id)


async def _forward(thread_id: Optional[str], content: Optional[str]) -> None:
    """on_message callback: route one delivered bubble to its WhatsApp chat.

    No route (e.g. after a restart lost the map) → log + drop, never crash the
    receive loop.
    """
    if not content:
        return
    route = _ROUTES.get(thread_id or "")
    if route is None:
        _log.warning("turn-taking: no route for thread %s — dropping bubble", thread_id)
        return
    adapter, chat_id = route
    _log.info("tt forward: tid=%s chat=%s → deliver bubble (via=%s) | %r",
              thread_id, chat_id, "orig" if _ORIG_SEND is not None else "plain", content[:60])
    # Deliver via the original send so the bubble bypasses our patch (which drops
    # the agent's draft). Before _patch_send runs, adapter.send IS the original.
    if _ORIG_SEND is not None:
        await _ORIG_SEND(adapter, chat_id, content)
    else:
        await adapter.send(chat_id, content)


async def _forward_typing(thread_id: Optional[str], is_typing: Optional[bool]) -> None:
    """on_typing callback: show "… is typing" on WhatsApp while the reply is paced.

    Easy version: fire a one-shot indicator on typing-start. WhatsApp presence
    auto-expires after a few seconds, so a long paced reply may stop showing it
    mid-way; refresh-on-a-timer is a later polish if that matters.
    """
    if not is_typing:
        return  # presence auto-expires; no explicit stop needed
    route = _ROUTES.get(thread_id or "")
    if route is None:
        return
    adapter, chat_id = route
    try:
        await adapter.send_typing(chat_id)
    except Exception as e:
        _log.warning("turn-taking typing failed: %s", e)


# ── Delivery bootstrap (chunk 8: open thread + route + start the WS loop) ──────
async def _start_delivery(
    adapter: Any, chat_id: str, thread_id: Optional[str] = None
) -> Optional[str]:
    """Open/reopen a thread, register its route, and start its WS receive loop.

    Returns the thread_id (use it for submit_messages / respond), or None if
    open_thread failed (fail-open: caller behaves as if turn-taking is off).

    ponytail: spawn-and-forget — assumes a stable connection (D2), so no task
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


# ── Session → thread map (chunk 9: one thread per conversation) ───────────────
_SESSIONS: Dict[str, str] = {}  # Hermes session_id → turn-taking thread_id
_OPEN_LOCK = asyncio.Lock()     # serializes thread-opens (rare) so a burst can't double-open


async def _ensure_thread(session_id: str, adapter: Any, chat_id: str) -> Optional[str]:
    """Get-or-create the turn-taking thread for a Hermes session.

    First activity per session opens the thread + WS delivery; later activity
    reuses it. Returns thread_id, or None if open_thread failed.

    Since messages are gated per-message (no batch), two first-messages of a new
    session can race; the lock + re-check makes the open once-only.
    """
    tid = _SESSIONS.get(session_id)
    if tid:
        return tid
    async with _OPEN_LOCK:
        tid = _SESSIONS.get(session_id)  # re-check under lock
        if tid:
            return tid
        tid = await _start_delivery(adapter, chat_id)
        if tid:
            _SESSIONS[session_id] = tid
        return tid


# ── Decide gate (chunk 10: submit a batch, decide, stash the speak epoch) ─────
# Per-turn epoch keyed by the platform message_id of the message that turn answers
# (D8). Keying by message_id — not by a single per-session slot — binds each draft
# to its OWN epoch at respond time: the transform hook recovers this turn's
# message_id from Hermes's per-task session contextvar (HERMES_SESSION_MESSAGE_ID,
# set by the gateway before the turn runs and propagated into the worker thread via
# copy_context). That is ordering-independent and needs no interrupt to stay correct.
_EPOCH_BY_MESSAGE_ID: Dict[str, int] = {}  # message_id → turn_epoch of the "speak" decision

# Per-turn message_id, carried into the turn's worker thread and read by the
# transform hook. Set inside GatewayRunner._run_agent (see _ensure_run_agent_patch),
# NOT in the inbound task: a queued message's turn runs via _run_agent's recursive
# follow-up (run.py:19091) INSIDE the prior turn's task, so the inbound task's set is
# never seen by it. _run_agent runs once per turn with that turn's own
# event_message_id, so setting the var there — before copy_context() snapshots it for
# the executor — gives every turn (first or queued) its correct id.
# (Hermes's own HERMES_SESSION_MESSAGE_ID is empty on the WhatsApp path, hence ours.)
_TT_MID_CTX: ContextVar[str] = ContextVar("tt_message_id", default="")
_run_agent_patched = False  # idempotency for _ensure_run_agent_patch

# The gateway's asyncio loop, captured on the inbound path (which runs in it).
# transform_llm_output fires in the agent's worker thread where there is NO running
# loop, so _respond must be scheduled onto this loop via run_coroutine_threadsafe —
# create_task there raises "no running event loop" and the naturalize call is lost.
_LOOP = None


async def _decide(
    session_id: str,
    adapter: Any,
    chat_id: str,
    messages: list[Dict[str, str]],
    system_prompt: Optional[str] = None,
    message_id: str = "",
) -> Optional[str]:
    """Submit a batch and return the decision ("speak" / "stay_silent").

    On "speak" the epoch is stashed in ``_EPOCH_BY_MESSAGE_ID[message_id]`` so the
    later ``respond`` for THIS turn can carry its own epoch (fail-closed). Returns
    None when turn-taking is unavailable (no thread / service error) — caller then
    behaves as if turn-taking is off (let Hermes reply normally).
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
        _EPOCH_BY_MESSAGE_ID[message_id] = epoch
    _log.info("tt decide: session=%s chat=%s mid=%s decision=%s epoch=%s",
              session_id, chat_id, message_id, decision, epoch)
    return decision


# ── Respond side (chunk 11: naturalize a draft, carry this turn's epoch) ───────
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
    tid = _SESSIONS.get(session_id)
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


# ── Hermes wiring (chunk 12: inbound events → service batch) ──────────────────
def _to_messages(events: list) -> list[Dict[str, str]]:
    """Convert Hermes inbound MessageEvents into the service's [{sender, content}].

    Duck-typed (event.text, event.source.user_name) so it needs no Hermes import.
    Skips empty text; applies the contract caps (≤20 messages, sender ≤255,
    content ≤4000). Pass a single event as ``[event]``.
    """
    out: list[Dict[str, str]] = []
    for ev in events:
        content = (getattr(ev, "text", "") or "").strip()
        if not content:
            continue
        sender = getattr(getattr(ev, "source", None), "user_name", None) or "Unknown"
        out.append({"sender": sender[:255], "content": content[:4000]})
    return out[-20:]


# ── Hermes wiring: monkeypatch WhatsAppAdapter.send ───────────────────────────
# chat_id → {exact answer strings awaiting their raw send}. transform_llm_output
# registers the FINAL answer's text here; the send patch drops the send whose
# content matches (it's naturalized + delivered over WS instead). Keying by
# CONTENT (not "next send") makes suppression order-independent: it targets the
# answer by identity, so a racing tool-notice / error send can't consume it.
_PENDING_ANSWERS: Dict[str, set] = {}


def _suppress_answer(chat_id: str, content: str) -> None:
    _PENDING_ANSWERS.setdefault(chat_id, set()).add((content or "").strip())


def _patch_send() -> bool:
    """Wrap ``WhatsAppAdapter.send``: turn the agent's draft into bubbles.

    When ``send`` carries the agent's reply for a "speak" turn, naturalize it
    (``_respond`` → bubbles delivered over WS) INSTEAD of sending the raw draft —
    that avoids the duplicate (whole draft + its split bubbles). Everything else
    passes straight through: errors, command replies, and — via ``_ORIG_SEND`` —
    the bubbles themselves.

    The "this is the draft" signal is ``_PENDING_ANSWERS`` (keyed by exact answer
    text), registered by the transform hook for the answer it naturalized; the send
    whose content matches is dropped, so a racing tool-notice can't be consumed by
    mistake. The draft still reaches Hermes history (persisted before send). Idempotent.
    """
    try:
        from gateway.platforms.whatsapp import WhatsAppAdapter
    except Exception as e:
        _log.warning("turn-taking: cannot patch send (no WhatsAppAdapter): %s", e)
        return False
    if getattr(WhatsAppAdapter.send, "_tt_patched", False):
        return False  # already patched
    global _ORIG_SEND
    _orig = WhatsAppAdapter.send
    _ORIG_SEND = _orig  # so _forward can deliver bubbles via the genuine send

    async def _send(self, chat_id, content, reply_to=None, metadata=None):
        pend = _PENDING_ANSWERS.get(chat_id)
        key = (content or "").strip()
        if pend is not None and key in pend:
            pend.discard(key)
            if not pend:
                _PENDING_ANSWERS.pop(chat_id, None)
            # This send IS the agent's final answer — naturalized by
            # transform_llm_output and delivered as bubbles over WS, so drop the
            # raw copy. Empty content → original returns a no-send success.
            _log.info("tt send: chat=%s → SUPPRESS (matched answer, bubbles via WS) | %r",
                      chat_id, key[:50])
            return await _orig(self, chat_id, "", reply_to=reply_to, metadata=metadata)
        _log.info("tt send: chat=%s → passthrough | %r", chat_id, key[:50])
        return await _orig(self, chat_id, content, reply_to=reply_to, metadata=metadata)

    _send._tt_patched = True  # type: ignore[attr-defined]
    WhatsAppAdapter.send = _send
    return True


# ── Inbound gate (chunk 18: stay_silent → keep context, don't reply) ──────────
def _persist_observed(session_store: Any, session_id: str, events: list) -> None:
    """Append inbound messages to Hermes history WITHOUT dispatching the agent.

    Used on a stay_silent decision so a later "speak" turn still has the context
    the bot stayed quiet on. ``observed: True`` marks the rows as context (they
    replay as background, not as unanswered user turns). The ``[Name]`` prefix is
    the Hermes-side authorship convention (D12), so the agent knows who said what.
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


# ── Inbound patch (chunk 21: gate each message, no debounce) ──────────────────
async def _handle_inbound(self: Any, event: Any) -> None:
    """Gate one inbound text message, then dispatch on "speak".

    No batching: the bridge poll already groups messages by its ~1s interval, and
    the service decides turn-taking per message (staying silent mid-burst, then
    speaking once). Messages the bot stays silent on are persisted as context by
    the gate. Hermes adds the ``[Name]`` prefix itself (run.py) for the dispatched
    message, so we don't merge.
    """
    global _LOOP
    try:
        _LOOP = asyncio.get_running_loop()  # capture the gateway loop for _respond scheduling
    except RuntimeError:
        pass
    _ensure_run_agent_patch(self)  # bind per-turn message_id inside _run_agent (queue-robust)
    source = event.source
    message_id = str(getattr(event, "message_id", "") or "")
    _log.info("tt inbound: chat=%s sender=%s mid=%s text=%r",
              getattr(source, "chat_id", None), getattr(source, "user_name", None),
              message_id, (getattr(event, "text", "") or "")[:50])
    store = getattr(self, "_session_store", None)
    try:
        if store is not None:
            session_id = store.get_or_create_session(source).session_id
            proceed = await _inbound_gate(
                self, store, session_id, source.chat_id, [event], _persona(self, session_id), message_id
            )
        else:
            proceed = True  # no store → can't decide; fall back to normal dispatch
    except Exception as e:
        _log.warning("turn-taking inbound gate failed: %s", e)
        proceed = True  # fail-open
    if proceed:
        await self.handle_message(event)


def _patch_inbound() -> bool:
    """Replace ``WhatsAppAdapter._enqueue_text_event``: gate each message instead
    of Hermes's 5s merge-debounce. Idempotent. Returns True if patched."""
    try:
        from gateway.platforms.whatsapp import WhatsAppAdapter
    except Exception as e:
        _log.warning("turn-taking: cannot patch inbound (no WhatsAppAdapter): %s", e)
        return False
    if getattr(WhatsAppAdapter._enqueue_text_event, "_tt_patched", False):
        return False

    def _enqueue_text_event(self, event):
        asyncio.create_task(_handle_inbound(self, event))

    _enqueue_text_event._tt_patched = True  # type: ignore[attr-defined]
    WhatsAppAdapter._enqueue_text_event = _enqueue_text_event
    return True


# ── Per-turn message_id binding (chunk 24: queue-robust correlation) ───────────
def _ensure_run_agent_patch(adapter: Any) -> None:
    """Wrap ``GatewayRunner._run_agent`` to stash this turn's message_id in
    ``_TT_MID_CTX`` for the transform hook to read.

    Why here and not in the inbound task: a message that arrives while the agent is
    busy is queued, and its turn is run later via ``_run_agent``'s recursive
    follow-up (run.py:19091) INSIDE the PRIOR turn's task — so a contextvar set in
    the inbound task never reaches it (it would read the prior turn's id). But
    ``_run_agent`` runs once per turn (first call run.py:9460, queued recursion
    run.py:19100), each with ``event_message_id`` = that turn's platform message_id
    (the WhatsApp reply anchor, which is ``event.message_id`` — the same id the
    decide side keyed the epoch map with). Setting the var at the top of
    ``_run_agent`` — in the turn's OWN coroutine frame, before ``copy_context()``
    snapshots it for the worker thread (run.py:15612) — gives every turn its correct
    id, queued or not. The token reset keeps it scoped to the call (so a recursive
    follow-up restores the parent's value on return).

    Reached via the adapter's message handler ``__self__`` (the GatewayRunner). Done
    once, lazily, when the gateway is live. Idempotent.
    """
    global _run_agent_patched
    if _run_agent_patched:
        return
    try:
        gw = getattr(getattr(adapter, "_message_handler", None), "__self__", None)
        if gw is None:
            return  # gateway not wired yet — retry on the next message
        cls = type(gw)
        orig = getattr(cls, "_run_agent", None)
        if orig is None:
            return
        if getattr(orig, "_tt_patched", False):
            _run_agent_patched = True
            return

        async def _wrapped(self, *args, **kwargs):
            token = _TT_MID_CTX.set(str(kwargs.get("event_message_id") or ""))
            try:
                return await orig(self, *args, **kwargs)
            finally:
                _TT_MID_CTX.reset(token)

        _wrapped._tt_patched = True  # type: ignore[attr-defined]
        cls._run_agent = _wrapped
        _run_agent_patched = True
        _log.info("tt: _run_agent wrapped (per-turn message_id binding)")
    except Exception as e:
        _log.warning("turn-taking: _run_agent patch deferred: %s", e)


# ── transform_llm_output hook: naturalize the FINAL answer, suppress its raw send ─
def _current_message_id() -> str:
    """The inbound platform message_id of the turn currently being answered.

    Read from our own per-task contextvar (``_TT_MID_CTX``), set per turn inside
    ``GatewayRunner._run_agent`` (see _ensure_run_agent_patch) and propagated into
    the worker thread via ``copy_context()``. Task-local, so concurrent turns never
    see each other's id, and queued follow-up turns get their OWN id. "" when
    unavailable (then we leave Hermes alone). (Hermes's own
    ``HERMES_SESSION_MESSAGE_ID`` is empty on the WhatsApp path — hence our var.)
    """
    return _TT_MID_CTX.get("") or ""


def _chat_for_session(session_id: str) -> Optional[str]:
    """The WhatsApp chat_id a session delivers to (session → thread → route)."""
    thread = _SESSIONS.get(session_id)
    route = _ROUTES.get(thread or "") if thread else None
    return route[1] if route else None


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
    epoch = _EPOCH_BY_MESSAGE_ID.pop(mid, None) if mid else None
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
    _coro = _respond(sid, draft, epoch, _persona(None, sid))
    try:
        if _LOOP is not None:
            asyncio.run_coroutine_threadsafe(_coro, _LOOP)  # bubbles delivered via WS
        else:
            asyncio.create_task(_coro)  # fallback: already in a loop
    except Exception as e:
        _coro.close()
        _log.warning("tt transform: could not schedule _respond: %s", e)
    return None


def register(ctx) -> None:
    """Plugin entry point: activate the send + inbound patches.

    Idle (no patching) when turn-taking isn't configured, so the plugin is a
    no-op unless ``TURN_TAKING_SERVICE_URL`` / config.yaml is set.
    """
    if not _service_url():
        _log.info("turn-taking: no service_url configured — plugin idle")
        return
    sent = _patch_send()
    inbound = _patch_inbound()
    hooked = False
    try:
        ctx.register_hook("transform_llm_output", on_transform_llm_output)
        hooked = True
    except Exception as e:
        _log.warning("turn-taking: could not register transform_llm_output hook: %s", e)
    _log.info("turn-taking registered (send=%s, inbound=%s, transform=%s)", sent, inbound, hooked)
