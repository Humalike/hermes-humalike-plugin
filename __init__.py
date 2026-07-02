"""Hermes turn-taking plugin — entry point and registration.

The implementation is split into three sibling packages this module wires
together:
  - ``turn_taking/``      the turn-taking machinery, itself split by concern:
      service (HTTP/WS I/O), state (shared runtime state), delivery (bubble
      delivery + thread lifecycle), core (decide/naturalize + system-prompt
      build), patching (the gateway monkeypatches), hooks (the plugin hooks)
  - ``social_learning/``  the per-conversation voice card (pre_llm_call hook)
  - ``soul/``             the SOUL.md persona feature (the /soul command)

This module only registers the plugin's command, hooks, and patches.
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import _config, social_learning, soul
from .turn_taking.hooks import on_transform_llm_output
from .turn_taking.patching import (
    _patch__enqueue_text_event,
    _patch_merge_pending_message_event,
    _patch__poll_messages,
    _patch_send,
    _patch_slack_handle_message,
    _patch_telegram_handle_message,
    _patch_telegram_observe_group,
)
from .turn_taking import notify
from .turn_taking.service import _service_url

_log = logging.getLogger(__name__)


def _warn_misconfig() -> None:
    """Fail loudly at startup on every misconfig that otherwise breaks silently.

    Each case produces broken behavior with no error near the cause (silent
    idle, 401s, per-user group sessions, streamed replies the plugin can't
    replace). Every problem is logged AND queued for the home chat, delivered on
    the first inbound message (adapters aren't connected yet here) — except when
    HUMALIKE_API_URL is unset, which leaves turn-taking off and no inbound path
    to flush through, so that one stays log-only.
    """
    problems = []
    if not _config.service_url():
        problems.append("HUMALIKE_API_URL is not set — turn-taking is DISABLED "
                        "(/soul still works). Add it to ~/.hermes/.env.")
    elif not _config.api_key():
        problems.append("HUMALIKE_API_KEY is not set — every Humalike call will "
                        "fail with 401. Add it to ~/.hermes/.env.")
    try:
        import yaml

        cfg = yaml.safe_load((Path.home() / ".hermes" / "config.yaml").read_text()) or {}
    except FileNotFoundError:
        cfg = {}
    except Exception as e:
        _log.warning("turn-taking: cannot read ~/.hermes/config.yaml (%s) — skipping config checks", e)
        cfg = None
    if cfg is not None:
        streaming = cfg.get("streaming")
        if streaming is True or (isinstance(streaming, dict) and streaming.get("enabled")):
            problems.append("streaming is enabled — set 'streaming: false' in "
                            "~/.hermes/config.yaml (the plugin must own the final reply).")
        if cfg.get("group_sessions_per_user", True):  # Hermes defaults this to true
            problems.append("group_sessions_per_user is not false — set "
                            "'group_sessions_per_user: false' (group chats need one shared thread).")
    for p in problems:
        _log.warning("turn-taking: %s", p)
    if problems:
        notify.queue_startup("⚠️ turn-taking misconfigured:\n• " + "\n• ".join(problems))


def register(ctx) -> None:
    """Plugin entry point: activate the send + inbound patches.

    Idle (no patching) when turn-taking isn't configured, so the plugin is a
    no-op unless ``HUMALIKE_API_URL`` is set — but the ``/soul`` persona
    command is registered regardless (it uses the separate Personas API, not
    the turn-taking service).
    """
    _warn_misconfig()
    try:
        ctx.register_command(
            "soul", soul.command,
            description="Enhance the agent's SOUL.md persona via Humalike",
            args_hint="enhance",
        )
        _log.info("turn-taking: registered /soul command")
    except Exception as e:
        _log.warning("turn-taking: could not register /soul command: %s", e)
    try:
        soul.maybe_auto_enhance()  # one-shot on first startup (marker-guarded)
    except Exception as e:
        _log.warning("turn-taking: auto-enhance skipped: %s", e)

    # Embedded social-learning voice card: register regardless of the turn-taking
    # patches (it no-ops while HUMALIKE_API_URL is unset and until a card is
    # cached). Fills the _CACHE that both the agent prompt (this
    # hook) and turn-taking's _build_system_prompt_for_turn_taking() read.
    try:
        ctx.register_hook("pre_llm_call", social_learning.on_pre_llm_call)
        _log.info("turn-taking: registered social-learning pre_llm_call hook")
    except Exception as e:
        _log.warning("turn-taking: could not register social-learning hook: %s", e)
    if not _service_url():
        return  # already warned loudly in _warn_misconfig()
    sent = _patch_send()
    inbound = _patch__enqueue_text_event()
    poll = _patch__poll_messages()
    tg_media = _patch_telegram_handle_message()
    tg_group = _patch_telegram_observe_group()
    slack = _patch_slack_handle_message()
    merge_fix = _patch_merge_pending_message_event()
    hooked = False
    try:
        ctx.register_hook("transform_llm_output", on_transform_llm_output)
        hooked = True
    except Exception as e:
        _log.warning("turn-taking: could not register transform_llm_output hook: %s", e)
    _log.info("turn-taking registered (send=%s, inbound=%s, poll=%s, tg_media=%s, tg_group=%s, slack=%s, merge_fix=%s, transform=%s)",
              sent, inbound, poll, tg_media, tg_group, slack, merge_fix, hooked)
