"""SOUL.md persona onboarding — `/soul enhance` chat command.

Self-contained on purpose: reads its own config/env and does its own HTTP, so it
never imports back into the plugin's __init__ (no circular import). The only thing
it borrows from the caller is the genuine ``send`` (passed in) so its replies
bypass the draft-suppression patch.

Backed by the Humalike Personas API:
  POST {base}/v1/personas/actions/enhance  {persona} -> {system_prompt, ...}

v1 scope: ENHANCE an existing SOUL.md only. Create-from-scratch is a later add.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

_log = logging.getLogger(__name__)

# HERMES_HOME-aware fallback, kept as a module attr so tests can repoint it.
_HERMES_HOME = Path(os.getenv("HERMES_HOME") or Path.home() / ".hermes")


def _hermes_home() -> Path:
    """The ACTIVE hermes home — the context-local profile override when profile
    routing has one in scope (per-turn, e.g. /soul enhance runs inside the
    gateway's _profile_runtime_scope), else the process default. Duplicated from
    turn_taking.service on purpose: this module stays import-free of the plugin."""
    try:
        from hermes_constants import get_hermes_home_override
    except ImportError:
        return _HERMES_HOME

    override = get_hermes_home_override()
    if override:
        return Path(override)
    return _HERMES_HOME


def _hermes_config() -> Path:
    return _hermes_home() / "config.yaml"


def _auto_marker() -> Path:
    return _hermes_home() / ".soul_auto_enhanced"  # one-shot guard, per profile
ENHANCE_PATH = "/v1/personas/actions/enhance"
ENHANCEMENT_REPO = "/v1/personas/repositories/Enhancement/by-id/{}"
DEFAULT_API = "https://api.humalike.com"

# Enhance is async server-side: POST returns {id, status:"pending"}, then we poll
# the repository route until it's "succeeded"/"failed". enhancement can run
# for minutes — ponytail: fixed ceiling of POLL_MAX*POLL_EVERY ≈ 5 min, then give up.
POLL_EVERY = 2.0
POLL_MAX = 150


# ── Config (own readers; env wins over config.yaml's turn_taking block) ───────
def _cfg() -> Dict[str, Any]:
    try:
        import yaml

        cfg = yaml.safe_load(_hermes_config().read_text()) or {}
        return cfg.get("turn_taking") or {}
    except Exception:
        return {}


def _getenv(name: str, default: str = "") -> str:
    """Env var, honoring Hermes's per-profile secret scope (mirrors _config._getenv;
    duplicated because this module stays import-free of the plugin). Unscoped
    lookup under multiplexing → default, never os.environ (fail closed)."""
    try:
        from agent.secret_scope import UnscopedSecretError, get_secret
    except ImportError:
        return os.getenv(name, default)
    try:
        val = get_secret(name, default)
    except UnscopedSecretError:
        return default
    return val if val is not None else default


def _api_url() -> str:
    url = _getenv("HUMALIKE_API_URL") or DEFAULT_API
    return url.rstrip("/")


def _api_key() -> str:
    # Same key as the turn-taking service unless a personas-specific one is set.
    return _getenv("HUMALIKE_API_KEY") or _getenv("TURN_TAKING_API_KEY")


def _soul_path() -> Path:
    """Where to read/write the persona. Default: ~/.hermes/SOUL.md (what the
    plugin's _build_system_prompt_for_turn_taking() actually reads at runtime). Override via env or
    ``turn_taking.soul_path`` in config.yaml (e.g. point it at docker/SOUL.md)."""
    p = os.getenv("HERMES_SOUL_PATH") or str(_cfg().get("soul_path") or "")
    return Path(p).expanduser() if p else _hermes_home() / "SOUL.md"


# ── SOUL.md parsing ───────────────────────────────────────────────────────────
def seed_body(raw: str) -> str:
    """The real persona text in a SOUL.md: HTML comments and markdown headings
    stripped. Empty string means only the template/boilerplate is present —
    nothing to enhance yet. (We still SEND the comment-free text; this is only
    the empty check.)"""
    no_comments = re.sub(r"<!--.*?-->", "", raw or "", flags=re.DOTALL)
    return "\n".join(
        ln for ln in no_comments.splitlines() if not ln.strip().startswith("#")
    ).strip()


def _persona_text(raw: str) -> str:
    """What we send to the enhance endpoint: the file minus HTML comments."""
    return re.sub(r"<!--.*?-->", "", raw or "", flags=re.DOTALL).strip()


# ── Enhance API ───────────────────────────────────────────────────────────────
# Appended to every persona we enhance so the generated system prompt never uses
# an em-dash (the LLM's tell). Sent in-band since enhance takes only {persona}.
_NO_EMDASH_DIRECTIVE = "\n\nHARD RULE: never use an em-dash (—) anywhere in this persona."


@dataclass(frozen=True)
class EnhanceResult:
    """The enhanced persona or a diagnostic failure outcome."""

    persona: Optional[Dict[str, Any]] = None
    error_kind: Optional[str] = None
    http_status: Optional[int] = None
    enhancement_id: Optional[str] = None


def _http_error_kind(status: int) -> str:
    if status == 401:
        return "auth"
    if status == 402:
        return "credits"
    if status == 403:
        return "forbidden"
    if status == 429:
        return "rate_limit"
    if status >= 500:
        return "service_unavailable"
    return "rejected"


def _error_message(result: EnhanceResult) -> str:
    if result.error_kind == "auth":
        return "⚠️ Humalike API key rejected — check HUMALIKE_API_KEY."
    if result.error_kind == "credits":
        return "⚠️ Not enough Humalike credits to enhance this persona."
    if result.error_kind == "forbidden":
        return "⚠️ Humalike denied this request — try again later or contact support."
    if result.error_kind == "rate_limit":
        return "⚠️ Too many requests — try again later."
    if result.error_kind == "service_unavailable":
        return "⚠️ Persona service is temporarily unavailable — try again later."
    if result.error_kind == "unreachable":
        return "⚠️ Couldn't reach the persona service — try again later."
    if result.error_kind == "result_not_visible":
        return "⚠️ Couldn't access the enhancement result — check API key configuration."
    if result.error_kind == "provider_failed":
        return "⚠️ Persona enhancement failed on our side — try again later."
    if result.error_kind == "timeout":
        return "⚠️ Persona enhancement is taking longer than five minutes — try again later."
    if result.error_kind == "invalid_response":
        return "⚠️ Persona service returned an invalid response."
    return f"⚠️ Persona service rejected this SOUL.md (HTTP {result.http_status})."


async def enhance(persona_text: str) -> EnhanceResult:
    """Enhance a persona, preserving the cause whenever it cannot complete.

    Server-side this is async: POST creates a job, then we poll the Enhancement
    repository until it reaches a terminal status.
    """
    base = _api_url()
    headers = {"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"}
    eid: Optional[str] = None
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                base + ENHANCE_PATH,
                json={"persona": persona_text + _NO_EMDASH_DIRECTIVE},
                headers=headers,
            )
            r.raise_for_status()
            try:
                created = r.json()
            except ValueError:
                created = None
            eid = created.get("id") if isinstance(created, dict) else None
            if not isinstance(eid, str) or not eid:
                _log.warning("soul: enhance POST returned no id: %s", r.text[:200])
                return EnhanceResult(error_kind="invalid_response")
            poll_url = base + ENHANCEMENT_REPO.format(eid)
            for _ in range(POLL_MAX):
                await asyncio.sleep(POLL_EVERY)
                p = await client.get(poll_url, headers=headers)
                p.raise_for_status()
                try:
                    data = p.json()
                except ValueError:
                    _log.warning("soul: enhancement %s returned invalid JSON", eid)
                    return EnhanceResult(error_kind="invalid_response", enhancement_id=eid)
                if data is None:  # ownership mismatch → repo returns null
                    _log.warning("soul: enhancement %s not visible (wrong API key?)", eid)
                    return EnhanceResult(error_kind="result_not_visible", enhancement_id=eid)
                if not isinstance(data, dict):
                    _log.warning("soul: enhancement %s returned invalid data", eid)
                    return EnhanceResult(error_kind="invalid_response", enhancement_id=eid)
                status = data.get("status")
                if status == "succeeded":
                    persona = data.get("persona")
                    if isinstance(persona, dict):
                        return EnhanceResult(persona=persona, enhancement_id=eid)
                    _log.warning("soul: enhancement %s succeeded without a persona", eid)
                    return EnhanceResult(error_kind="invalid_response", enhancement_id=eid)
                if status == "failed":
                    _log.warning("soul: enhance %s failed: %s", eid, data.get("error"))
                    return EnhanceResult(error_kind="provider_failed", enhancement_id=eid)
            _log.warning("soul: enhance %s timed out after ~%ds", eid, int(POLL_MAX * POLL_EVERY))
            return EnhanceResult(error_kind="timeout", enhancement_id=eid)
    except httpx.HTTPStatusError as e:
        _log.warning("soul: enhance → HTTP %s: %s", e.response.status_code, e.response.text[:200])
        return EnhanceResult(
            error_kind=_http_error_kind(e.response.status_code),
            http_status=e.response.status_code,
            enhancement_id=eid,
        )
    except httpx.HTTPError as e:
        _log.warning("soul: enhance unreachable: %s", e)
        return EnhanceResult(error_kind="unreachable")


# ── Command handler ───────────────────────────────────────────────────────────
# Registered via ctx.register_command("soul", ...). The gateway calls this with the
# args after "/soul" and sends our return string back to the chat. v1: only
# "enhance" (bare /soul defaults to it). No source/chat is passed to plugin command
# handlers, so this can't be DM-gated — anyone in a chat with the bot can run it.
async def command(raw_args: str) -> str:
    """`/soul enhance` — deepen the agent's SOUL.md persona via the Personas API.

    Reads SOUL.md, enhances it, backs the old file up to SOUL.md.bak, writes the
    enhanced system prompt back, and returns a status line (the reply)."""
    sub = (raw_args or "").strip().lower()
    if sub and not sub.startswith("enhance"):
        return "Usage: /soul enhance — deepen your agent's SOUL.md persona."

    path = _soul_path()
    try:
        raw = path.read_text()
    except FileNotFoundError:
        raw = ""
    if not seed_body(raw):
        return ("Your SOUL.md has no persona to enhance yet — add a few lines describing your "
                "agent, then send /soul enhance. (Generating one from scratch is coming soon.)")

    result = await enhance(_persona_text(raw))
    if result.error_kind:
        return _error_message(result) + " SOUL.md left unchanged."
    enhanced = (result.persona or {}).get("system_prompt")
    if not isinstance(enhanced, str) or not enhanced.strip():
        return "⚠️ Persona service returned an invalid response. SOUL.md left unchanged."

    enhanced = enhanced.strip()
    try:
        Path(str(path) + ".bak").write_text(raw)
        path.write_text(enhanced + "\n")
    except Exception as e:
        _log.warning("soul: write failed: %s", e)
        return f"⚠️ Enhanced, but couldn't write {path}: {e}"

    _log.info("soul: enhanced %s (%d → %d chars)", path, len(raw), len(enhanced))
    return (f"✅ Enhanced your persona ({len(raw)} → {len(enhanced)} chars). "
            f"Old version saved to {path.name}.bak — it takes effect on your next message.")


# ── Auto-enhance on first startup ─────────────────────────────────────────────
# There is no install-time hook in Hermes; register() runs at every gateway boot.
# A per-SOUL.md state file claims the one automatic attempt before its thread starts.
# Failures are terminal for automatic runs; use /soul enhance to retry manually.
def _auto_enabled() -> bool:
    v = os.getenv("HERMES_SOUL_AUTO_ENHANCE")
    if v is None:
        v = _cfg().get("soul_auto_enhance")
    if v is None:
        return True  # default on
    return str(v).strip().lower() not in ("false", "0", "no", "off")


def _auto_state_path(soul_path: Path) -> Path:
    digest = hashlib.sha256(str(soul_path.resolve()).encode()).hexdigest()
    return _hermes_home() / "soul-auto-enhance" / f"{digest}.json"


def _auto_state(status: str) -> str:
    return json.dumps(
        {"status": status, "attempted_at": datetime.now(timezone.utc).isoformat()},
        separators=(",", ":"),
    )


def _claim_auto_state(marker: Path) -> bool:
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        with marker.open("x") as state:
            state.write(_auto_state("pending"))
    except FileExistsError:
        return False
    return True


def _read_auto_state(marker: Path) -> Optional[str]:
    try:
        status = json.loads(marker.read_text()).get("status")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    return status if isinstance(status, str) else None


def _write_auto_state(marker: Path, status: str) -> None:
    temporary = marker.with_name(f".{marker.name}.{threading.get_ident()}.tmp")
    try:
        temporary.write_text(_auto_state(status))
        temporary.replace(marker)
    finally:
        if temporary.exists():
            temporary.unlink()


async def _auto_enhance(path: Path) -> bool:
    """One-shot enhance with no chat to reply to — log the outcome. Returns True
    only when SOUL.md was actually rewritten (so the caller sets the once-marker)."""
    try:
        raw = path.read_text()
    except FileNotFoundError:
        raw = ""
    if not seed_body(raw):
        _log.info("soul: auto-enhance skipped — %s has no persona seed yet", path)
        return False
    result = await enhance(_persona_text(raw))
    enhanced = (result.persona or {}).get("system_prompt")
    if not isinstance(enhanced, str) or not enhanced.strip():
        _log.warning(
            "soul: auto-enhance failed kind=%s status=%s id=%s — %s left unchanged",
            result.error_kind, result.http_status, result.enhancement_id, path,
        )
        return False
    enhanced = enhanced.strip()
    try:
        Path(str(path) + ".bak").write_text(raw)
        path.write_text(enhanced + "\n")
    except Exception as e:
        _log.warning("soul: auto-enhance write failed: %s", e)
        return False
    _log.info("soul: auto-enhanced %s (%d → %d chars)", path, len(raw), len(enhanced))
    return True


def maybe_auto_enhance() -> Optional[str]:
    """Schedule one automatic attempt per resolved SOUL.md path without blocking boot."""
    if not _auto_enabled():
        return
    path = _soul_path().resolve()
    try:
        raw = path.read_text()
    except FileNotFoundError:
        raw = ""
    if not seed_body(raw):
        _log.info("soul: auto-enhance skipped — %s has no persona seed yet", path)
        return

    marker = _auto_state_path(path)
    try:
        claimed = _claim_auto_state(marker)
    except OSError as e:
        _log.warning("soul: auto-enhance state unavailable for %s: %s", path, e)
        return None
    if not claimed:
        status = _read_auto_state(marker)
        if status == "failed":
            _log.info("soul: auto-enhance previously failed for %s — use /soul enhance to retry", path)
        elif status in ("pending", "succeeded"):
            _log.info("soul: auto-enhance already %s for %s", status, path)
            if status == "pending":
                return (f"⚠️ Persona auto-enhancement is still pending for {path.name} — "
                        "use /soul enhance to retry.")
        else:
            _log.warning("soul: auto-enhance state unreadable for %s — not retrying automatically", path)
            return (f"⚠️ Persona auto-enhancement state is unreadable for {path.name} — "
                    "use /soul enhance to retry.")
        return

    # Snapshot the context NOW: the background thread outlives any context-local
    # profile scope, so home-dependent paths (SOUL.md, the marker) must resolve
    # under the scope that scheduled us, not the thread's empty context.
    import contextvars

    ctx = contextvars.copy_context()

    def _run() -> None:
        try:
            status = "succeeded" if ctx.run(asyncio.run, _auto_enhance(path)) else "failed"
            ctx.run(_write_auto_state, marker, status)
        except Exception as e:
            try:
                ctx.run(_write_auto_state, marker, "failed")
            except OSError:
                pass
            _log.warning("soul: auto-enhance thread errored: %s", e)

    threading.Thread(target=_run, daemon=True).start()
    _log.info("soul: auto-enhance scheduled for %s", path)
