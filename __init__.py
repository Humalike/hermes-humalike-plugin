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
from .turn_taking.service import _service_url

_log = logging.getLogger("hermes.plugins.turn_taking")


def _warn_misconfig() -> None:
    """Fail loudly at startup on every misconfig that otherwise breaks silently.

    Each case below produces broken behavior with no error anywhere near the
    cause (silent idle, 401s, per-user group sessions, streamed replies the
    plugin can't replace) — so name the fix in the log line itself.
    """
    if not _config.service_url():
        _log.warning(
            "turn-taking: HUMALIKE_API_URL is not set — turn-taking is DISABLED "
            "(/soul still works). Add it to ~/.hermes/.env and restart the gateway."
        )
    elif not _config.api_key():
        _log.warning(
            "turn-taking: HUMALIKE_API_KEY is not set — every Humalike call will "
            "fail with 401. Add it to ~/.hermes/.env and restart the gateway."
        )
    try:
        import yaml

        cfg = yaml.safe_load((Path.home() / ".hermes" / "config.yaml").read_text()) or {}
    except FileNotFoundError:
        cfg = {}
    except Exception as e:
        _log.warning("turn-taking: cannot read ~/.hermes/config.yaml (%s) — skipping config checks", e)
        return
    streaming = cfg.get("streaming")
    if streaming is True or (isinstance(streaming, dict) and streaming.get("enabled")):
        _log.warning(
            "turn-taking: streaming is enabled — the plugin must own the final reply "
            "text and will misbehave. Set 'streaming: false' in ~/.hermes/config.yaml."
        )
    if cfg.get("group_sessions_per_user", True):  # Hermes defaults this to true
        _log.warning(
            "turn-taking: group_sessions_per_user is not false — group chats get one "
            "session per member instead of one shared thread, so the bot loses the "
            "conversation flow. Set 'group_sessions_per_user: false' in ~/.hermes/config.yaml."
        )


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
