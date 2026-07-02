"""Social-learning voice card — embedded in the turn-taking plugin.

Matches the agent's reply style to how each conversation actually talks. Lives
here (not as a separate plugin) so the one Humalike plugin owns it: the same
``_CACHE`` feeds BOTH the agent's reply (via the ``pre_llm_call`` hook below) and
turn-taking's decide/naturalize voice (via ``_build_system_prompt_for_turn_taking()`` in core.py).

Two clocks in one hook
======================
SLOW CLOCK (refresh): every REFRESH_EVERY turns a daemon thread POSTs the recent
transcript to ``{service_url}/v1/social-learning/extract``; the service returns a
``prompt_block`` ("voice card") cached under _LOCK. Failures are discarded.

FAST CLOCK (inject): every call reads _CACHE and returns ``{"context": card}`` so
Hermes injects the card into the agent prompt. No card yet → None (LLM unchanged).

Config
------
Env: ``HUMALIKE_API_URL`` + ``HUMALIKE_API_KEY`` (shared with all sub-plugins;
unset URL disables refresh). Optional ``social_learning.log_requests: true`` in
config.yaml dumps request payloads to JSONL.

ponytail: no back-off, last-writer-wins — acceptable for a style hint. Uses
httpx (already a plugin dep), not requests. Cache is persisted to a JSON file
(_CACHE_FILE) so voice cards survive a Hermes restart; see _load_cache /
_save_cache.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

REFRESH_EVERY: int = 5
WINDOW: int = 100
SERVICE_PATH: str = "/v1/social-learning/actions/extract"

_LOCK: threading.Lock = threading.Lock()
_CACHE: Dict[str, str] = {}   # session_id -> prompt_block (voice card)
_COUNTER: Dict[str, int] = {}  # session_id -> turn count


def _cache_file():
    from hermes_constants import get_hermes_home  # noqa: PLC0415

    return get_hermes_home() / "state" / "social-learning-cache.json"


def _load_cache() -> None:
    """Best-effort restore of _CACHE/_COUNTER from disk. Never raises."""
    try:
        path = _cache_file()
        data = json.loads(path.read_text(encoding="utf-8"))
        _CACHE.update(data.get("cache", {}))
        _COUNTER.update(data.get("counter", {}))
        logger.info("social-learning: restored %d cached voice card(s) from %s", len(_CACHE), path)
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.debug("social-learning: cache restore failed: %s", exc)


def _save_cache() -> None:
    """Persist _CACHE/_COUNTER to disk (atomic write). Caller holds _LOCK. Never raises."""
    try:
        path = _cache_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"cache": _CACHE, "counter": _COUNTER}), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:
        logger.debug("social-learning: cache save failed: %s", exc)


_load_cache()


# ── Config helpers ────────────────────────────────────────────────────────────
# Shared with turn-taking: one HUMALIKE_API_URL + HUMALIKE_API_KEY for every Humalike call.
from .. import _config  # noqa: E402
from ..turn_taking import notify  # noqa: E402


def _get_service_url() -> str:
    """Base URL (``HUMALIKE_API_URL``)."""
    return _config.service_url()


def _log_requests_enabled() -> bool:
    """Whether to dump request payloads to JSONL (off unless ``social_learning.log_requests``)."""
    try:
        from hermes_cli.config import load_config, cfg_get  # noqa: PLC0415

        return bool(cfg_get(load_config(), "social_learning", "log_requests", default=False))
    except Exception:
        return False


# ── Transcript parsing ────────────────────────────────────────────────────────
# Hermes prefixes group messages with "[Sender] " and injects control markers;
# we lift the "[Name]" prefix into a per-line author and drop the markers so the
# service sees attributed messages, not one "user" blob.
_AUTHOR_RE = re.compile(r"^\[([^\]]{1,60})\]\s*(.*)$")
_CONTROL_PREFIXES = (
    "[New message]",
    "[Observed Telegram group context",
    "[Current addressed message",
    "[User sent ",
    "[The user sent ",
    "[Delivered from ",
    "[IMPORTANT:",
)


def _is_control_marker(line: str) -> bool:
    return any(line.startswith(p) for p in _CONTROL_PREFIXES)


def _parse_messages(content: str) -> List[Dict[str, str]]:
    """Split one Hermes user-turn into per-speaker, per-line transcript messages."""
    out: List[Dict[str, str]] = []
    author = "user"
    for raw in content.split("\n"):
        line = raw.strip()
        if not line or _is_control_marker(line):
            continue
        m = _AUTHOR_RE.match(line)
        if m:
            author = m.group(1).strip() or "user"
            text = m.group(2).strip()
        else:
            text = line
        if text:
            out.append({"author": author, "text": text})
    return out


def _build_transcript(conversation_history: Any) -> List[Dict[str, str]]:
    """Filter + parse conversation_history into the service message format (last WINDOW msgs)."""
    if not conversation_history:
        return []
    messages: List[Dict[str, str]] = []
    for msg in conversation_history:
        if (
            isinstance(msg, dict)
            and msg.get("role") == "user"
            and isinstance(msg.get("content"), str)
            and msg["content"]
        ):
            messages.extend(_parse_messages(msg["content"]))
    windowed = messages[-WINDOW:]
    # Wire field is ``speaker`` (social_norms Message schema), not ``author``.
    return [
        {"id": str(idx), "speaker": m["author"], "text": m["text"]}
        for idx, m in enumerate(windowed)
    ]


# ── Slow-clock worker (detached daemon thread) ───────────────────────────────
def _log_request(session_id: str, url: str, body: Dict[str, Any]) -> None:
    """Append the outgoing request payload to a JSONL file (debug only). Never raises."""
    if not _log_requests_enabled():
        return
    try:
        from datetime import datetime  # noqa: PLC0415

        from hermes_constants import get_hermes_home  # noqa: PLC0415

        path = get_hermes_home() / "logs" / "social-learning-requests.jsonl"
        record = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "session_id": session_id,
            "url": url,
            "message_count": len(body.get("transcript", {}).get("messages", [])),
            "body": body,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.debug("social-learning: request log failed: %s", exc)


def _refresh_card(session_id: str, conversation_history: Any) -> None:
    """Fetch a fresh voice card from the service and cache it. Runs in a daemon
    thread; all exceptions are swallowed so a failure never reaches the agent loop."""
    try:
        transcript = _build_transcript(conversation_history)
        if not transcript:
            return
        base = _get_service_url()
        if not base:
            return
        api_key = _config.api_key()
        url = base + SERVICE_PATH
        body = {"transcript": {"messages": transcript}}
        logger.info(
            "social-learning: POST %s (msgs=%d, api_key=%s) for session %s",
            url, len(transcript), "set" if api_key else "MISSING", session_id,
        )
        _log_request(session_id, url, body)
        response = httpx.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body,
            timeout=60,
        )
        if response.status_code == 200:
            try:
                data = response.json()
            except Exception as exc:
                logger.debug("social-learning: bad JSON for session %s: %s", session_id, exc)
                return
            prompt_block = data.get("prompt_block")
            if isinstance(prompt_block, str) and prompt_block:
                with _LOCK:
                    _CACHE[session_id] = prompt_block
                    _save_cache()
                logger.info(
                    "social-learning: refreshed voice card for session %s (%d chars from %d msgs)",
                    session_id, len(prompt_block), len(transcript),
                )
        else:
            logger.info(
                "social-learning: non-200 %d for session %s (skipping, retry next cycle)",
                response.status_code, session_id,
            )
            notify.alert_social(status=response.status_code)
    except Exception as exc:
        logger.warning("social-learning: _refresh_card failed for session %s: %s", session_id, exc)
        notify.alert_social(exc)


# ── Hook ──────────────────────────────────────────────────────────────────────
def on_pre_llm_call(**kwargs: Any) -> Optional[Dict[str, Any]]:
    """pre_llm_call hook: inject the voice card and (every REFRESH_EVERY turns) refresh it."""
    try:
        session_id: str = kwargs.get("session_id") or ""
        if not session_id:
            return None
        conversation_history = kwargs.get("conversation_history") or []

        with _LOCK:
            _COUNTER[session_id] = _COUNTER.get(session_id, 0) + 1
            n = _COUNTER[session_id]
            has_card = session_id in _CACHE

        logger.debug(
            "social-learning: turn %d session=%s card=%s (refresh every %d)",
            n, session_id, "cached" if has_card else "none", REFRESH_EVERY,
        )

        if n % REFRESH_EVERY == 0 and _get_service_url():
            logger.info("social-learning: turn %d session %s — firing detached refresh", n, session_id)
            threading.Thread(
                target=_refresh_card,
                args=(session_id, list(conversation_history)),
                daemon=True,
            ).start()

        with _LOCK:
            card = _CACHE.get(session_id)
        if card:
            logger.info("social-learning: injecting voice card for session %s (%d chars)", session_id, len(card))
            return {"context": card}
        return None
    except Exception as exc:
        logger.warning("social-learning: on_pre_llm_call failed: %s", exc)
        return None
