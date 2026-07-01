"""The monkeypatches that splice turn-taking into the gateway.

The Hermes-coupled surface — the only place that imports ``gateway.*`` and rebinds
adapter/runner methods. Each patch defers its ``gateway`` import to call time so
the module imports fine without a live gateway. Behaviour lives in ``core`` /
``delivery``; this file only re-routes Hermes's calls through them.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from . import state
from .core import _inbound_gate, _build_system_prompt_for_turn_taking, _decide, _delivery_meta
from .service import _to_messages

_log = logging.getLogger("hermes.plugins.turn_taking")

_reply_anchor_patched = False  # idempotency for _patch__reply_anchor_for_event
_merge_patched = False  # idempotency for _patch_merge_pending_message_event


def _platform_adapter_classes() -> list:
    """The adapter classes turn-taking patches, those importable in this gateway.

    Each platform is imported independently so a build without one (e.g. no
    Telegram deps) just skips it. Both inherit ``BasePlatformAdapter`` and share
    the ``send`` / ``_enqueue_text_event`` surface, so the same wrappers fit both.
    """
    classes = []
    try:
        from plugins.platforms.whatsapp.adapter import WhatsAppAdapter
        classes.append(WhatsAppAdapter)
    except Exception as e:
        _log.warning("turn-taking: WhatsAppAdapter unavailable: %s", e)
    try:
        from plugins.platforms.telegram.adapter import TelegramAdapter
        classes.append(TelegramAdapter)
    except Exception as e:
        _log.warning("turn-taking: TelegramAdapter unavailable: %s", e)
    return classes


# ── Hermes wiring: monkeypatch each adapter's send ────────────────────────────
def _patch_send() -> bool:
    """Wrap each platform adapter's ``send``: turn the agent's draft into bubbles.

    When ``send`` carries the agent's reply for a "speak" turn, naturalize it
    (``_respond`` → bubbles delivered over WS) INSTEAD of sending the raw draft —
    that avoids the duplicate (whole draft + its split bubbles). Everything else
    passes straight through: errors, command replies, and — via
    ``state.ORIG_SEND`` — the bubbles themselves.

    The "this is the draft" signal is ``PENDING_ANSWERS`` (keyed by exact answer
    text), registered by the transform hook for the answer it naturalized; the send
    whose content matches is dropped, so a racing tool-notice can't be consumed by
    mistake. The draft still reaches Hermes history (persisted before send).

    Patches every importable adapter class, each wrapper closing over its OWN
    class's original via a factory so a Telegram bubble is never sent through
    WhatsApp's send and vice versa. Idempotent.
    """
    patched_any = False
    for cls in _platform_adapter_classes():
        if getattr(cls.send, "_tt_patched", False):
            continue
        _orig = cls.send
        state.ORIG_SEND[cls] = _orig  # so _forward delivers bubbles via the genuine send

        def _make_send(orig):
            async def _send(self, chat_id, content, reply_to=None, metadata=None):
                pend = state.PENDING_ANSWERS.get(chat_id)
                key = (content or "").strip()
                if pend is not None and key in pend:
                    pend.discard(key)
                    if not pend:
                        state.PENDING_ANSWERS.pop(chat_id, None)
                    # This send IS the agent's final answer — naturalized by
                    # transform_llm_output and delivered as bubbles over WS, so drop
                    # the raw copy. Empty content → original returns a no-send success.
                    _log.info("tt send: chat=%s → SUPPRESS (matched answer, bubbles via WS) | %r",
                              chat_id, key[:50])
                    return await orig(self, chat_id, "", reply_to=reply_to, metadata=metadata)
                _log.info("tt send: chat=%s → passthrough | %r", chat_id, key[:50])
                return await orig(self, chat_id, content, reply_to=reply_to, metadata=metadata)
            return _send

        _send = _make_send(_orig)
        _send._tt_patched = True  # type: ignore[attr-defined]
        cls.send = _send
        patched_any = True
    return patched_any


# ── Inbound patch: gate each message, no debounce ─────────────────────────────
async def _handle_inbound(self: Any, event: Any) -> None:
    """Gate one inbound text message, then dispatch on "speak".

    No batching: the bridge poll already groups messages by its ~1s interval, and
    the service decides turn-taking per message (staying silent mid-burst, then
    speaking once). Messages the bot stays silent on are persisted as context by
    the gate. Hermes adds the ``[Name]`` prefix itself (run.py) for the dispatched
    message, so we don't merge.
    """
    try:
        state.LOOP = asyncio.get_running_loop()  # capture the gateway loop for _respond scheduling
    except RuntimeError:
        pass
    _patch__reply_anchor_for_event(self)  # bind per-turn raw message_id (queue-robust)
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
                self, store, session_id, source.chat_id, [event], _build_system_prompt_for_turn_taking(self, session_id), message_id
            )
        else:
            proceed = True  # no store → can't decide; fall back to normal dispatch
    except Exception as e:
        _log.warning("turn-taking inbound gate failed: %s", e)
        proceed = True  # fail-open
    if proceed:
        # Dispatch a "speak" turn through the GENUINE handler. On a platform whose
        # handle_message is gated (Telegram, for media), self.handle_message is our
        # gate — calling it would recurse; ORIG_HANDLE holds the real one. WhatsApp
        # has no entry (its handle_message is unpatched) → use it directly.
        orig = state.ORIG_HANDLE.get(type(self))
        if orig is not None:
            await orig(self, event)
        else:
            await self.handle_message(event)


def _patch__enqueue_text_event() -> bool:
    """Replace each adapter's ``_enqueue_text_event``: gate each message instead
    of Hermes's merge-debounce. Patches every importable adapter class (the gate is
    platform-agnostic — it duck-types the event). Idempotent. Returns True if any
    class was patched."""
    patched_any = False
    for cls in _platform_adapter_classes():
        if getattr(cls._enqueue_text_event, "_tt_patched", False):
            continue

        def _enqueue_text_event(self, event):
            asyncio.create_task(_handle_inbound(self, event))

        _enqueue_text_event._tt_patched = True  # type: ignore[attr-defined]
        cls._enqueue_text_event = _enqueue_text_event
        patched_any = True
    return patched_any


def _patch__poll_messages() -> bool:
    """Route EVERY inbound event through the turn-taking gate, not just text.

    Hermes's ``_poll_messages`` sends TEXT to ``_enqueue_text_event`` (the gate,
    patched above) but dispatches media straight to ``handle_message`` — so
    images/gifs/videos/voice/docs bypass turn-taking entirely. This replaces the
    loop so every built event goes through ``_enqueue_text_event``; ``_to_messages``
    flags media with a placeholder so the service speaks and the reply naturalizes.
    The event keeps its real ``message_type``/``media_urls``, so the agent still
    gets the actual media (vision/STT/doc handling unchanged). Idempotent.

    ponytail: this copies Hermes's poll loop (HTTP poll + bridge-exit checks +
    error handling); if hermes-agent changes that loop, re-sync this copy. Safe to
    patch on the class because plugins register (run.py:4419) before adapters
    connect and start this task (run.py:4535).
    """
    try:
        from plugins.platforms.whatsapp.adapter import WhatsAppAdapter
    except Exception as e:
        _log.warning("turn-taking: cannot patch poll loop (no WhatsAppAdapter): %s", e)
        return False
    if getattr(WhatsAppAdapter._poll_messages, "_tt_patched", False):
        return False

    async def _poll_messages(self) -> None:
        import aiohttp

        while self._running:
            if not self._http_session:
                break
            bridge_exit = await self._check_managed_bridge_exit()
            if bridge_exit:
                print(f"[{self.name}] {bridge_exit}")
                break
            try:
                async with self._http_session.get(
                    f"http://127.0.0.1:{self._bridge_port}/messages",
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status == 200:
                        messages = await resp.json()
                        for msg_data in messages:
                            event = await self._build_message_event(msg_data)
                            if event:
                                # Text AND media → the gate. _enqueue_text_event is
                                # patched to create_task(_handle_inbound).
                                self._enqueue_text_event(event)
            except asyncio.CancelledError:
                break
            except Exception as e:
                bridge_exit = await self._check_managed_bridge_exit()
                if bridge_exit:
                    print(f"[{self.name}] {bridge_exit}")
                    break
                print(f"[{self.name}] Poll error: {e}")
                await asyncio.sleep(5)

            await asyncio.sleep(1)  # Poll interval

    _poll_messages._tt_patched = True  # type: ignore[attr-defined]
    WhatsAppAdapter._poll_messages = _poll_messages
    return True


# ── Telegram media gate: route handle_message through the turn-taking gate ─────
def _patch_telegram_handle_message() -> bool:
    """Gate Telegram media by routing ``TelegramAdapter.handle_message`` through the
    same gate as text.

    Telegram text is gated at ``_enqueue_text_event``, but media
    (photos/voice/docs/video) is dispatched straight to ``handle_message``, so it
    would bypass turn-taking. Wrapping ``handle_message`` sends every such event
    through ``_handle_inbound``; the genuine handler is captured in
    ``state.ORIG_HANDLE`` so a "speak" turn dispatches via it instead of recursing.

    Telegram-only: WhatsApp routes media through the patched ``_poll_messages``
    loop, so its ``handle_message`` stays unpatched (and ``_handle_inbound`` falls
    back to it directly). Idempotent.
    """
    try:
        from plugins.platforms.telegram.adapter import TelegramAdapter
    except Exception as e:
        _log.warning("turn-taking: cannot patch Telegram media gate (no TelegramAdapter): %s", e)
        return False
    if getattr(TelegramAdapter.handle_message, "_tt_patched", False):
        return False
    _orig = TelegramAdapter.handle_message  # inherited from base; genuine handler
    state.ORIG_HANDLE[TelegramAdapter] = _orig

    async def handle_message(self, event):
        await _handle_inbound(self, event)

    handle_message._tt_patched = True  # type: ignore[attr-defined]
    TelegramAdapter.handle_message = handle_message
    return True


# ── Group observation gate: let turn-taking jump into unmentioned chatter ──────
async def _handle_observed_group(self: Any, event: Any, replay: Callable[[], Any]) -> None:
    """Gate one unmentioned group message: dispatch on an explicit "speak",
    otherwise fall back to the genuine observe (``replay``) so it's persisted as
    context.

    Fail-CLOSED on purpose (unlike the DM gate, which fails open to a reply):
    anything but an explicit "speak" — stay_silent OR service unavailable — routes
    to observe, so a down turn-taking service can't make the bot answer every group
    message. Correlation/epoch are stashed by ``_decide`` exactly as on the DM path.
    """
    try:
        state.LOOP = asyncio.get_running_loop()
    except RuntimeError:
        pass
    _patch__reply_anchor_for_event(self)
    message_id = str(getattr(event, "message_id", "") or "")
    source = event.source
    decision = None
    store = getattr(self, "_session_store", None)
    if store is not None:
        try:
            session_id = store.get_or_create_session(source).session_id
            messages = _to_messages([event])
            if messages:
                decision = await _decide(
                    session_id, self, source.chat_id, messages,
                    _build_system_prompt_for_turn_taking(self, session_id), message_id,
                    _delivery_meta(event),
                )
        except Exception as e:
            _log.warning("turn-taking observed-group gate failed: %s", e)
            decision = None
    _log.info("tt group-observe: chat=%s mid=%s decision=%s",
              getattr(source, "chat_id", None), message_id, decision)
    if decision == "speak":
        orig = state.ORIG_HANDLE.get(type(self))
        if orig is not None:
            await orig(self, event)
        else:
            await self.handle_message(event)
    else:
        replay()  # genuine observe: persist as context (fail-closed)


def _patch_telegram_observe_group() -> bool:
    """Route unmentioned Telegram group messages through the turn-taking gate.

    ``_observe_unmentioned_group_message`` is the single funnel every untriggered
    group message (text/media/location) reaches. Wrapping it lets turn-taking
    decide to jump in unprompted (speak → dispatch) while keeping the original
    silent-observe behaviour for everything else. Telegram-only; idempotent.

    ponytail: gating observed chatter opens a turn-taking thread per active group
    even when the bot mostly stays silent — the cost of "see everything". If that
    bites in busy groups, gate thread-open on a cheaper pre-check.
    """
    try:
        from plugins.platforms.telegram.adapter import TelegramAdapter
    except Exception as e:
        _log.warning("turn-taking: cannot patch Telegram group observe (no TelegramAdapter): %s", e)
        return False
    if getattr(TelegramAdapter._observe_unmentioned_group_message, "_tt_patched", False):
        return False
    _orig = TelegramAdapter._observe_unmentioned_group_message

    def _observe(self, message, msg_type, update_id=None, event=None):
        try:
            ev = event or self._build_message_event(message, msg_type, update_id=update_id)
        except Exception:
            return _orig(self, message, msg_type, update_id=update_id, event=event)
        replay = lambda: _orig(self, message, msg_type, update_id=update_id, event=ev)
        asyncio.create_task(_handle_observed_group(self, ev, replay))

    _observe._tt_patched = True  # type: ignore[attr-defined]
    TelegramAdapter._observe_unmentioned_group_message = _observe
    return True


# ── Per-turn message_id binding: queue-robust correlation ─────────────────────
def _patch__reply_anchor_for_event(adapter: Any) -> None:
    """Wrap ``GatewayRunner._reply_anchor_for_event`` to stash this turn's RAW
    ``event.message_id`` in ``state.TT_MID_CTX`` for the transform hook to read.

    Why HERE and not in ``_run_agent``: the decide side keys the epoch map by the
    raw ``event.message_id``, so the respond side must look it up by the SAME id.
    But ``_run_agent`` only receives ``event_message_id`` = the reply ANCHOR
    (``_reply_anchor_for_event(event)``), which is ``None`` on Telegram forum-topic
    groups — there the lookup misses and the bot stays silent. The raw event isn't
    in ``_run_agent``'s params at all.

    Both turn paths, however, compute the anchor via ``self._reply_anchor_for_event``
    with the raw event in scope, immediately before ``_run_agent`` — the first turn
    at run.py:9460 (``event``) and the queued follow-up at run.py:19075
    (``pending_event``). Wrapping this one small staticmethod therefore covers both,
    queue-robustly, in the turn's own frame before ``copy_context()`` snapshots the
    context for the worker thread.

    We do NOT change the return value (Hermes still gets the genuine anchor for its
    reply-threading, ``None`` on topics included); we only bind ``event.message_id``
    as a side effect. Reached via the adapter's message handler ``__self__`` (the
    GatewayRunner). Done once, lazily, when the gateway is live. Idempotent.
    """
    global _reply_anchor_patched
    if _reply_anchor_patched:
        return
    try:
        gw = getattr(getattr(adapter, "_message_handler", None), "__self__", None)
        if gw is None:
            return  # gateway not wired yet — retry on the next message
        cls = type(gw)
        orig = getattr(cls, "_reply_anchor_for_event", None)
        if orig is None:
            return
        if getattr(orig, "_tt_patched", False):
            _reply_anchor_patched = True
            return

        def _wrapped(event):
            # Side effect: bind THIS turn's raw message_id for the transform hook.
            # The return value stays the genuine anchor — Hermes uses it for reply.
            try:
                state.TT_MID_CTX.set(str(getattr(event, "message_id", "") or ""))
            except Exception:
                pass
            return orig(event)

        _wrapped._tt_patched = True  # type: ignore[attr-defined]
        cls._reply_anchor_for_event = staticmethod(_wrapped)
        _reply_anchor_patched = True
        _log.info("tt: _reply_anchor_for_event wrapped (per-turn raw message_id binding)")
    except Exception as e:
        _log.warning("turn-taking: _reply_anchor_for_event patch deferred: %s", e)


# ── Merged-turn epoch fix: keep the LATEST id when text is merged ──────────────
def _patch_merge_pending_message_event() -> bool:
    """Make a turn formed by merging rapid follow-ups keep the LATEST message_id.

    When the agent is busy, Hermes coalesces follow-up TEXT into the pending turn
    via ``merge_pending_message_event(merge_text=True)``. That function's text
    branch appends the text but NEVER reassigns ``existing.message_id`` — so the
    merged turn carries the FIRST message's id. The plugin then binds the FIRST
    (already superseded) turn-taking epoch → the service drops it → SILENCE, even
    though the merged turn computed the correct (latest) answer.

    Fix: after the merge, rebind the surviving event's id to the LATEST — exactly
    what the debounce path already does for its own buffer (base.py:3491-3496).
    Separate turns never enter the merge branch, so they are unaffected. Patched in
    BOTH modules that name the function: ``gateway.platforms.base`` (defines it) and
    ``gateway.run`` (imported it by value), else run's call sites keep the old one.
    Idempotent.
    """
    global _merge_patched
    if _merge_patched:
        return False
    try:
        import gateway.platforms.base as _base
        import gateway.run as _run
    except Exception as e:
        _log.warning("turn-taking: cannot patch merge (import failed): %s", e)
        return False
    orig = getattr(_base, "merge_pending_message_event", None)
    if orig is None:
        return False
    if getattr(orig, "_tt_patched", False):
        _merge_patched = True
        return False

    def _merge(pending_messages, session_key, event, *, merge_text=False):
        existing = pending_messages.get(session_key)
        orig(pending_messages, session_key, event, merge_text=merge_text)
        # A text follow-up merged INTO the existing pending turn (same object kept,
        # not replaced) → keep the latest id so the merged turn binds the current
        # epoch. Guard on a real id so synthetic/interrupt events (id=None) can't
        # clobber it.
        merged = pending_messages.get(session_key)
        if (
            merge_text
            and merged is existing
            and merged is not None
            and getattr(event, "message_id", None) is not None
        ):
            mid = str(event.message_id)
            merged.message_id = mid
            if hasattr(merged, "reply_to_message_id"):
                merged.reply_to_message_id = mid

    _merge._tt_patched = True  # type: ignore[attr-defined]
    _base.merge_pending_message_event = _merge
    _run.merge_pending_message_event = _merge  # run.py imported it by value
    _merge_patched = True
    _log.info("tt: merge_pending_message_event wrapped (keep latest id)")
    return True
