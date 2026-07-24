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
import logging
import os
import re
import threading
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

        override = get_hermes_home_override()
        if override:
            return Path(override)
    except Exception:
        pass
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


def _api_url() -> str:
    url = os.getenv("HUMALIKE_API_URL") or DEFAULT_API
    return url.rstrip("/")


def _api_key() -> str:
    # Same key as the turn-taking service unless a personas-specific one is set.
    return os.getenv("HUMALIKE_API_KEY") or os.getenv("TURN_TAKING_API_KEY", "")


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


async def enhance(persona_text: str) -> Optional[Dict[str, Any]]:
    """Enhance a persona and return the rendered ``persona`` dict (with
    ``system_prompt``/``fields``/``markdown``), or None on any failure (fail-open).

    Server-side this is async: POST creates a job, then we poll the Enhancement
    repository until it reaches a terminal status.
    """
    base = _api_url()
    headers = {"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                base + ENHANCE_PATH,
                json={"persona": persona_text + _NO_EMDASH_DIRECTIVE},
                headers=headers,
            )
            r.raise_for_status()
            eid = (r.json() or {}).get("id")
            if not eid:
                _log.warning("soul: enhance POST returned no id: %s", r.text[:200])
                return None
            poll_url = base + ENHANCEMENT_REPO.format(eid)
            for _ in range(POLL_MAX):
                await asyncio.sleep(POLL_EVERY)
                p = await client.get(poll_url, headers=headers)
                p.raise_for_status()
                data = p.json()
                if data is None:  # ownership mismatch → repo returns null
                    _log.warning("soul: enhancement %s not visible (wrong API key?)", eid)
                    return None
                status = data.get("status")
                if status == "succeeded":
                    return data.get("persona")
                if status == "failed":
                    _log.warning("soul: enhance %s failed: %s", eid, data.get("error"))
                    return None
            _log.warning("soul: enhance %s timed out after ~%ds", eid, int(POLL_MAX * POLL_EVERY))
            return None
    except httpx.HTTPStatusError as e:
        _log.warning("soul: enhance → HTTP %s: %s", e.response.status_code, e.response.text[:200])
        return None
    except httpx.HTTPError as e:
        _log.warning("soul: enhance unreachable: %s", e)
        return None


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

    persona = await enhance(_persona_text(raw))
    enhanced = (persona or {}).get("system_prompt")
    if not enhanced:
        return "⚠️ Couldn't reach the persona service — SOUL.md left unchanged."

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
# So "auto-run after install" = run once, guarded by a marker file. The marker is
# written only after a *successful* enhance, so a boot where the service is down or
# SOUL.md has no seed yet harmlessly retries next time. Disable with
# `turn_taking.soul_auto_enhance: false` (or env HERMES_SOUL_AUTO_ENHANCE=false).
def _auto_enabled() -> bool:
    v = os.getenv("HERMES_SOUL_AUTO_ENHANCE")
    if v is None:
        v = _cfg().get("soul_auto_enhance")
    if v is None:
        return True  # default on
    return str(v).strip().lower() not in ("false", "0", "no", "off")


async def _auto_enhance() -> bool:
    """One-shot enhance with no chat to reply to — log the outcome. Returns True
    only when SOUL.md was actually rewritten (so the caller sets the once-marker)."""
    path = _soul_path()
    try:
        raw = path.read_text()
    except FileNotFoundError:
        raw = ""
    if not seed_body(raw):
        _log.info("soul: auto-enhance skipped — %s has no persona seed yet", path)
        return False
    persona = await enhance(_persona_text(raw))
    enhanced = (persona or {}).get("system_prompt")
    if not enhanced:
        _log.warning("soul: auto-enhance failed (service unreachable?) — %s left unchanged", path)
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


def maybe_auto_enhance() -> None:
    """Fire the one-shot auto-enhance on first startup, in a background thread so it
    never blocks gateway boot (enhance polls for minutes). Marker-guarded and a no-op
    once done. ponytail: delete ~/.hermes/.soul_auto_enhanced to force a re-run."""
    if not _auto_enabled() or _auto_marker().exists():
        return

    # Snapshot the context NOW: the background thread outlives any context-local
    # profile scope, so home-dependent paths (SOUL.md, the marker) must resolve
    # under the scope that scheduled us, not the thread's empty context.
    import contextvars

    ctx = contextvars.copy_context()

    def _run() -> None:
        try:
            if ctx.run(asyncio.run, _auto_enhance()):
                ctx.run(_auto_marker).write_text("")  # succeeded → never auto-run again
        except Exception as e:
            _log.warning("soul: auto-enhance thread errored: %s", e)

    threading.Thread(target=_run, daemon=True).start()
    _log.info("soul: auto-enhance scheduled (first startup)")
