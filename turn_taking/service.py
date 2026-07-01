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
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

import httpx

_log = logging.getLogger("hermes.plugins.turn_taking")

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

_HERMES_CONFIG = Path.home() / ".hermes" / "config.yaml"


# ── Config + auth ─────────────────────────────────────────────────────────────
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
    """API key from ``TURN_TAKING_API_KEY`` (sent as ``Authorization: Bearer``)."""
    return os.getenv("TURN_TAKING_API_KEY", "")


def _headers() -> Dict[str, str]:
    """Auth + content-type headers for every service call."""
    return {"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"}


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
) -> Optional[Dict[str, Any]]:
    """Naturalize a draft (epoch required, fail-closed). Returns {scheduled, superseded}.

    ``metadata`` is an opaque per-turn bag the service echoes verbatim on every
    delivered bubble frame (svc contract ``RespondRequest.metadata``, cap 4 KB) —
    used here to carry the forum-topic id to ``_forward``.
    """
    body: Dict[str, Any] = {"thread_id": thread_id, "content": content, "turn_epoch": turn_epoch}
    if system_prompt:
        body["system_prompt"] = system_prompt[:_SYSTEM_PROMPT_CAP]
    if metadata:
        body["metadata"] = metadata
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
        content = (getattr(ev, "text", "") or "").strip()
        mtype = getattr(getattr(ev, "message_type", None), "value", "") or ""
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
