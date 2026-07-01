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

from . import social_learning, soul
from .turn_taking.hooks import on_transform_llm_output
from .turn_taking.patching import (
    _patch__enqueue_text_event,
    _patch_merge_pending_message_event,
    _patch__poll_messages,
    _patch_send,
    _patch_telegram_handle_message,
    _patch_telegram_observe_group,
)
from .turn_taking.service import _service_url

_log = logging.getLogger("hermes.plugins.turn_taking")


def register(ctx) -> None:
    """Plugin entry point: activate the send + inbound patches.

    Idle (no patching) when turn-taking isn't configured, so the plugin is a
    no-op unless ``TURN_TAKING_SERVICE_URL`` / config.yaml is set — but the
    ``/soul`` persona command is registered regardless (it uses the separate
    Personas API, not the turn-taking service).
    """
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
    # service (it's gated on its own ``social_learning.service_url`` and no-ops
    # until a card is cached). Fills the _CACHE that both the agent prompt (this
    # hook) and turn-taking's _build_system_prompt_for_turn_taking() read.
    try:
        ctx.register_hook("pre_llm_call", social_learning.on_pre_llm_call)
        _log.info("turn-taking: registered social-learning pre_llm_call hook")
    except Exception as e:
        _log.warning("turn-taking: could not register social-learning hook: %s", e)
    if not _service_url():
        _log.info("turn-taking: no service_url configured — turn-taking idle (/soul still available)")
        return
    sent = _patch_send()
    inbound = _patch__enqueue_text_event()
    poll = _patch__poll_messages()
    tg_media = _patch_telegram_handle_message()
    tg_group = _patch_telegram_observe_group()
    merge_fix = _patch_merge_pending_message_event()
    hooked = False
    try:
        ctx.register_hook("transform_llm_output", on_transform_llm_output)
        hooked = True
    except Exception as e:
        _log.warning("turn-taking: could not register transform_llm_output hook: %s", e)
    _log.info("turn-taking registered (send=%s, inbound=%s, poll=%s, tg_media=%s, tg_group=%s, merge_fix=%s, transform=%s)",
              sent, inbound, poll, tg_media, tg_group, merge_fix, hooked)
