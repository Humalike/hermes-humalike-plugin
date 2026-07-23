"""Turn-taking service I/O — config/auth, HTTP transport, the action calls, the
WebSocket framing, and the inbound→wire message mapping.

Pure transport layer: it knows the service's wire contract and nothing about
Hermes, adapters, or the plugin's runtime state. Everything stateful (routes,
sessions, epochs, the monkeypatches) lives in ``__init__.py`` and calls in here.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

import httpx

from . import notify

_log = logging.getLogger(__name__)

# ── Wire contract (turn-taking service action paths) ──────────────────────────
OPEN_THREAD_PATH = "/v1/turn-taking/actions/open_thread"
SUBMIT_PATH = "/v1/turn-taking/actions/submit_messages"
RESPOND_PATH = "/v1/turn-taking/actions/respond"

# The service rejects an over-long system_prompt (validation_failed) and 422s
# every decide/respond, silently disabling turn-taking. Clamp at the wire to the
# service's contract cap (svc-turn-taking contracts.py max_length) as a safety net
# for an oversized SOUL.md + voice card. Head-keep: identity is at the top of
# SOUL.md, the most useful signal for decide/naturalize.
_SYSTEM_PROMPT_CAP = 100000

# ~/.hermes/config.yaml — used by core.py to read SOUL.md + the agent system prompt.
_HERMES_CONFIG = Path.home() / ".hermes" / "config.yaml"


# ── Config + auth ─────────────────────────────────────────────────────────────
# Shared across all Humalike sub-plugins: one HUMALIKE_API_URL + HUMALIKE_API_KEY.
from .. import _config  # noqa: E402


def _service_url() -> str:
    """Base URL (``HUMALIKE_API_URL``)."""
    return _config.service_url()


def _api_key() -> str:
    """API key, sent as ``Authorization: Bearer`` (``HUMALIKE_API_KEY``)."""
    return _config.api_key()


def _headers() -> Dict[str, str]:
    """Auth + content-type headers for every service call."""
    return {"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"}


# The plugin's own pacing default: a bit under the service's stock 150 wpm —
# the humanlike product wants a slightly more deliberate typist out of the box.
# Only typing_wpm is pinned; reading delay and the per-message cap stay with
# the service (it fills every field the request leaves unset).
_DEFAULT_PACING: Dict[str, Any] = {"typing_wpm": 115}


def _pacing() -> Dict[str, Any]:
    """Per-reply pacing sent on every respond (svc ``PacingOverrides``:
    reading_delay_ms, typing_wpm, max_typing_ms — each optional, absent =
    service default). ``turn_taking.pacing`` in config.yaml wins when set to a
    non-empty mapping; otherwise ``_DEFAULT_PACING``. Read per call like
    SOUL.md, so a config edit paces the very next reply, no restart."""
    try:
        import yaml

        cfg = yaml.safe_load(_HERMES_CONFIG.read_text()) or {}
        pacing = (cfg.get("turn_taking") or {}).get("pacing")
        if isinstance(pacing, dict) and pacing:
            return dict(pacing)
    except Exception:
        pass
    return dict(_DEFAULT_PACING)


# ── Transport ─────────────────────────────────────────────────────────────────
async def _post(path: str, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """POST ``body`` to ``path`` and return parsed JSON, or None on any failure.

    Fail-open by design: an unconfigured URL or any network/HTTP error returns
    None so a service blip never wedges the gateway — callers fall back to
    "behave as if turn-taking is off".
    """
    base = _service_url()
    if not base:
        return None
    if not _api_key():
        # Keyless: never ship conversation content off-machine just to collect
        # a 401. Fail-open exactly like an unreachable service; /connect puts
        # the key live in-process, so this un-gates without a restart.
        return None
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(base + path, json=body, headers=_headers())
            r.raise_for_status()
            notify.recovered()  # success → clear any active alert, post recovery once
            return r.json()
    except httpx.HTTPStatusError as e:
        # 4xx = our request is wrong (bad key/payload) — actionable, log loud.
        # 5xx = service broken. Either way fail-open, but never silent.
        _log.warning(
            "turn-taking %s → HTTP %s: %s", path, e.response.status_code, e.response.text[:200]
        )
        notify.alert(e)
        return None
    except httpx.HTTPError as e:
        # connect refused / DNS / timeout — service unreachable, fail-open.
        _log.warning("turn-taking %s unreachable: %s", path, e)
        notify.alert(e)
        return None
    # ponytail: a non-JSON 2xx (r.json() → JSONDecodeError) and our own bugs
    # (KeyError/TypeError) now propagate instead of hiding as fail-open.


# ── Actions ───────────────────────────────────────────────────────────────────
async def open_thread(thread_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Open/reopen a thread (id = idempotency key). Returns {thread, channel, realtime}."""
    return await _post(OPEN_THREAD_PATH, {"thread_id": thread_id} if thread_id else {})


async def submit_messages(
    thread_id: str,
    messages: list[Dict[str, Any]],
    system_prompt: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Decide speak/stay_silent for a batch of {sender, content, has_media?}. Returns {decision, turn_epoch}."""
    body: Dict[str, Any] = {"thread_id": thread_id, "messages": messages}
    if system_prompt:
        body["system_prompt"] = system_prompt[:_SYSTEM_PROMPT_CAP]
    return await _post(SUBMIT_PATH, body)


async def respond(
    thread_id: str,
    content: str,
    turn_epoch: int,
    system_prompt: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    agent_name: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Naturalize a draft (epoch required, fail-closed). Returns {scheduled, superseded}.

    ``metadata`` is an opaque per-turn bag the service echoes verbatim on every
    delivered bubble frame (svc contract ``RespondRequest.metadata``, cap 4 KB) —
    used here to carry the forum-topic id to ``_forward``.

    ``agent_name`` is the bot's display name (svc contract
    ``RespondRequest.agent_name``, cap 255): the service uses it to label the
    bot's own lines in the theory-of-mind transcript so the model doesn't
    confuse the bot with an interlocutor (third-person replies, replying to
    itself). Omitted → the service's generic internal label.
    """
    body: Dict[str, Any] = {"thread_id": thread_id, "content": content, "turn_epoch": turn_epoch}
    if system_prompt:
        body["system_prompt"] = system_prompt[:_SYSTEM_PROMPT_CAP]
    if metadata:
        body["metadata"] = metadata
    if agent_name:
        body["agent_name"] = agent_name[:255]
    body["pacing"] = _pacing()
    return await _post(RESPOND_PATH, body)


# ── WS lifecycle: parse the open_thread grant ─────────────────────────────────
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
    on_message: Callable[[Optional[str], Optional[str], Optional[Dict[str, Any]]], Awaitable[None]],
    on_typing: Optional[Callable[[Optional[str], Optional[bool]], Awaitable[None]]] = None,
) -> None:
    """Read envelopes until the socket closes; dispatch each by ``type``.

    - ``turn_taking.message`` → ``await on_message(thread_id, content, metadata)`` (one bubble;
      ``metadata`` is the verbatim echo of the respond's ``metadata``, or None)
    - ``turn_taking.typing``  → ``await on_typing(thread_id, typing)`` (if provided)
    - ``attached`` (handshake) → ignored

    No reconnect: assumes a stable connection. On close/error it logs and
    returns; the supervising task decides what to do next.
    """
    try:
        import websockets
    except Exception as e:  # dependency missing
        _log.warning("turn-taking WS unavailable (no websockets lib): %s", e)
        notify.alert(e, notify.WS_LOST, kind="ws")
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
                    await on_message(data.get("thread_id"), data.get("content"), data.get("metadata"))
                elif t == "turn_taking.typing" and on_typing is not None:
                    await on_typing(data.get("thread_id"), data.get("typing"))
    except Exception as e:
        _log.warning("turn-taking WS loop ended: %s", e)
        notify.alert(e, notify.WS_LOST, kind="ws")


# ── Hermes wiring: inbound events → service batch ─────────────────────────────
# Placeholder content sent to the decide service for media with no caption, keyed
# by MessageType.value. The decide model can't read media; ``has_media`` makes the
# service short-circuit to "speak" without an LLM call. A non-empty content also
# keeps the contract's min_length=1 and gives later text turns some context.
_MEDIA_PLACEHOLDERS = {
    "photo": "[image]",
    "video": "[video]",
    "voice": "[voice message]",
    "audio": "[audio]",
    "document": "[document]",
    "sticker": "[sticker]",
}


def _to_messages(events: list) -> list[Dict[str, Any]]:
    """Convert Hermes inbound MessageEvents into the service's
    [{sender, content, has_media?}].

    Duck-typed (event.text, event.source.user_name, event.message_type) so it
    needs no Hermes import. Text events with empty text are skipped; media events
    are kept with a placeholder content + ``has_media=True`` so the service speaks
    (no LLM call) and the reply still naturalizes. Applies the contract caps
    (≤20 messages, sender ≤255, content ≤4000). Pass a single event as ``[event]``.
    """
    out: list[Dict[str, Any]] = []
    for ev in events:
        # ``_tt_content`` (set by patching._annotate_mentions) has <@id> mentions
        # resolved to @you / @Name; fall back to the raw text when it's absent.
        content = (getattr(ev, "_tt_content", None) or getattr(ev, "text", "") or "").strip()
        mtype = getattr(getattr(ev, "message_type", None), "value", "") or ""
        # Discord fills captionless media with a host placeholder sentence instead
        # of empty text; treat it as empty so the [image]/[media] branch applies
        # and the service transcript isn't polluted with a meta-note.
        if content == "(The user sent a message with no text content)":
            content = ""
        # WhatsApp only emits "text" or a media type (commands arrive as text), so
        # anything else — or any attached media — marks this as a media message.
        has_media = bool(getattr(ev, "media_urls", None)) or mtype not in ("", "text", "command")
        if not content:
            if not has_media:
                continue
            content = _MEDIA_PLACEHOLDERS.get(mtype, "[media]")
        sender = getattr(getattr(ev, "source", None), "user_name", None) or "Unknown"
        msg: Dict[str, Any] = {"sender": sender[:255], "content": content[:4000]}
        if has_media:
            msg["has_media"] = True
        out.append(msg)
    return out[-20:]
