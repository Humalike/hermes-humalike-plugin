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
) -> None:
    """Read envelopes until the socket closes; dispatch each by ``type``.

    - ``turn_taking.message`` → ``await on_message(thread_id, content)`` (one bubble)
    - ``attached`` (handshake) and ``turn_taking.typing`` → ignored (chunk 7 may
      drive a typing indicator off the latter)

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
            async for frame in ws:
                try:
                    env = json.loads(frame)
                except Exception:
                    continue
                if env.get("type") == "turn_taking.message":
                    data = env.get("data") or {}
                    await on_message(data.get("thread_id"), data.get("content"))
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
    # Deliver via the original send so the bubble bypasses our patch (which drops
    # the agent's draft). Before _patch_send runs, adapter.send IS the original.
    if _ORIG_SEND is not None:
        await _ORIG_SEND(adapter, chat_id, content)
    else:
        await adapter.send(chat_id, content)


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
    asyncio.create_task(_receive_loop(url, _forward))
    return tid


# ── Session → thread map (chunk 9: one thread per conversation) ───────────────
_SESSIONS: Dict[str, str] = {}  # Hermes session_id → turn-taking thread_id


async def _ensure_thread(session_id: str, adapter: Any, chat_id: str) -> Optional[str]:
    """Get-or-create the turn-taking thread for a Hermes session.

    First activity per session opens the thread + WS delivery; later activity
    reuses it. Returns thread_id, or None if open_thread failed.

    ponytail: check-then-await-set has a thin race if two first-messages for the
    same NEW session interleave (→ two threads). The inbound micro-batch (later
    chunk) coalesces simultaneous messages into one submit, so in practice the
    first call wins; add a per-session lock only if that proves insufficient.
    """
    tid = _SESSIONS.get(session_id)
    if tid:
        return tid
    tid = await _start_delivery(adapter, chat_id)
    if tid:
        _SESSIONS[session_id] = tid
    return tid


# ── Decide gate (chunk 10: submit a batch, decide, stash the speak epoch) ─────
_EPOCH: Dict[str, int] = {}  # session_id → turn_epoch of the last "speak" (D8)


async def _decide(
    session_id: str,
    adapter: Any,
    chat_id: str,
    messages: list[Dict[str, str]],
    system_prompt: Optional[str] = None,
) -> Optional[str]:
    """Submit a batch and return the decision ("speak" / "stay_silent").

    On "speak" the epoch is stashed in ``_EPOCH[session_id]`` so the later
    ``respond`` can carry it (fail-closed). Returns None when turn-taking is
    unavailable (no thread / service error) — caller then behaves as if
    turn-taking is off (let Hermes reply normally).
    """
    tid = await _ensure_thread(session_id, adapter, chat_id)
    if not tid:
        return None
    res = await submit_messages(tid, messages, system_prompt)
    if not res:
        return None
    decision = res.get("decision")
    if decision == "speak":
        _EPOCH[session_id] = res.get("turn_epoch")
    return decision


# ── Respond side (chunk 11: naturalize a draft, carry the stashed epoch) ───────
async def _respond(session_id: str, draft: str, system_prompt: Optional[str] = None) -> bool:
    """Naturalize a completed draft for a session that decided "speak".

    Reads (and consumes) the stashed epoch and calls respond. In WS mode the
    bubbles are delivered by the receive loop, so nothing is returned for
    sending — the bool just says whether a reply was scheduled:

    - True  → scheduled; bubbles will arrive over WS.
    - False → never decided speak / superseded (newer batch won) / service error.
    """
    tid = _SESSIONS.get(session_id)
    epoch = _EPOCH.pop(session_id, None)  # consume once
    if not tid or epoch is None:
        return False  # this session never decided speak
    res = await respond(tid, draft, epoch, system_prompt)
    if not res or res.get("superseded"):
        return False  # dropped: a newer batch arrived, or service error
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


# ── Hermes wiring (chunk 13: monkeypatch WhatsAppAdapter.send) ────────────────
def _patch_send() -> bool:
    """Wrap ``WhatsAppAdapter.send`` so we can intercept outbound replies.

    Chunk 13: passthrough only — proves the interception works and is idempotent.
    A later chunk replaces the body with "call _respond() and suppress the native
    send for turn-taking sessions". Returns True if it patched, False otherwise.
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
        # chunk 13: passthrough. Later: _respond(draft) + suppress for tt sessions.
        return await _orig(self, chat_id, content, reply_to=reply_to, metadata=metadata)

    _send._tt_patched = True  # type: ignore[attr-defined]
    WhatsAppAdapter.send = _send
    return True


def register(ctx) -> None:
    """Entry point. Wire hooks / adapter patches here (later chunks)."""
    pass


if __name__ == "__main__":  # offline self-check (no network) — config layer only
    os.environ["TURN_TAKING_SERVICE_URL"] = "http://x:8008/"
    os.environ["TURN_TAKING_API_KEY"] = "ak_test"
    assert _service_url() == "http://x:8008", _service_url()
    assert _headers()["Authorization"] == "Bearer ak_test"
    _resp = {
        "thread": {"id": "562640fb-03e9-47da-8afc-8702ff20bfee"},
        "channel": "turn-taking-thread/562640fb-03e9-47da-8afc-8702ff20bfee",
        "realtime": {"connect_url": "ws://localhost:8005/v1/ws/turn-taking-thread?token=eyJ"},
    }
    assert _thread_id(_resp) == "562640fb-03e9-47da-8afc-8702ff20bfee"
    assert _connect_url(_resp).startswith("ws://localhost:8005/")
    assert _thread_id(None) is None and _connect_url({}) is None  # malformed → None

    # chunk 7: forwarder routing (offline, fake adapter)
    class _FakeAdapter:
        def __init__(self) -> None:
            self.sent: list = []

        async def send(self, chat_id, content):
            self.sent.append((chat_id, content))

    async def _check_forward():
        fa = _FakeAdapter()
        _set_route("th1", fa, "chatA")
        await _forward("th1", "hej")        # routed
        await _forward("th1", "")           # empty → skipped
        await _forward("unknown", "x")      # no route → dropped, no crash
        return fa.sent

    assert asyncio.run(_check_forward()) == [("chatA", "hej")]
    _ROUTES.clear()

    # chunk 14: with _ORIG_SEND set, _forward delivers via the original (bypasses patch)
    _orig_calls: list = []

    async def _fake_orig(adapter, chat_id, content):
        _orig_calls.append((chat_id, content))

    globals()["_ORIG_SEND"] = _fake_orig
    _fa2 = _FakeAdapter()
    _set_route("th2", _fa2, "chatB")
    asyncio.run(_forward("th2", "elo"))
    assert _orig_calls == [("chatB", "elo")] and _fa2.sent == []  # original used, not adapter.send
    globals()["_ORIG_SEND"] = None
    _ROUTES.clear()

    # chunk 9: session→thread dedup (stub _start_delivery, count opens)
    _starts: list = []

    async def _fake_start(adapter, chat_id, thread_id=None):
        _starts.append(chat_id)
        return f"th-{len(_starts)}"

    globals()["_start_delivery"] = _fake_start

    async def _check_sessions():
        t1 = await _ensure_thread("sessA", None, "chatA")
        t2 = await _ensure_thread("sessA", None, "chatA")  # reuse, no new open
        t3 = await _ensure_thread("sessB", None, "chatB")  # new
        return t1, t2, t3

    _t1, _t2, _t3 = asyncio.run(_check_sessions())
    assert _t1 == _t2 and _t1 != _t3, (_t1, _t2, _t3)
    assert _starts == ["chatA", "chatB"], _starts  # sessA opened once
    _SESSIONS.clear()

    # chunk 10: decide gate (stub ensure_thread + submit_messages via a holder)
    _reply: dict = {}  # mutable holder the submit stub returns; set per case

    async def _stub_ensure(session_id, adapter, chat_id):
        return "th-x"

    async def _stub_submit(thread_id, messages, system_prompt=None):
        return _reply.get("v")

    globals()["_ensure_thread"] = _stub_ensure
    globals()["submit_messages"] = _stub_submit

    async def _decide_case(value):
        _reply["v"] = value
        return await _decide("sX", None, "c", [{"sender": "M", "content": "hej"}])

    assert asyncio.run(_decide_case({"decision": "speak", "turn_epoch": 5})) == "speak"
    assert _EPOCH.get("sX") == 5  # speak stashes the epoch
    _EPOCH.clear()
    assert asyncio.run(_decide_case({"decision": "stay_silent", "turn_epoch": 6})) == "stay_silent"
    assert "sX" not in _EPOCH  # stay_silent does not stash
    assert asyncio.run(_decide_case(None)) is None  # service unavailable → None
    _EPOCH.clear()
    _SESSIONS.clear()

    # chunk 11: respond side (stub respond via holder, record the epoch passed)
    _rresp: dict = {}  # holder: {"v": <respond return>, "epoch": <captured>}

    async def _stub_respond(thread_id, content, turn_epoch, system_prompt=None):
        _rresp["epoch"] = turn_epoch
        return _rresp.get("v")

    globals()["respond"] = _stub_respond

    async def _respond_case(value):
        _rresp["v"] = value
        return await _respond("sY", "draft")

    # no epoch stashed → no respond, False
    _SESSIONS["sY"] = "th-y"
    assert asyncio.run(_respond_case({"scheduled": [{}]})) is False
    # speak epoch present → scheduled, True, epoch consumed + passed through
    _EPOCH["sY"] = 9
    assert asyncio.run(_respond_case({"scheduled": [{}], "superseded": False})) is True
    assert _rresp["epoch"] == 9 and "sY" not in _EPOCH
    # superseded → dropped, False
    _EPOCH["sY"] = 10
    assert asyncio.run(_respond_case({"scheduled": [], "superseded": True})) is False
    _EPOCH.clear()
    _SESSIONS.clear()

    # chunk 12: inbound events → service batch
    class _Src:
        def __init__(self, name):
            self.user_name = name

    class _Ev:
        def __init__(self, text, name):
            self.text = text
            self.source = _Src(name)

    assert _to_messages([_Ev("hej", "Maks"), _Ev("  ", "X"), _Ev("elo", "Borrell")]) == [
        {"sender": "Maks", "content": "hej"},
        {"sender": "Borrell", "content": "elo"},
    ]
    assert _to_messages([_Ev("yo", None)]) == [{"sender": "Unknown", "content": "yo"}]
    assert len(_to_messages([_Ev(f"m{i}", "U") for i in range(30)])) == 20  # cap
    print("ok")
