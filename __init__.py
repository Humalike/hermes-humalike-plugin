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
import os
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


_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}


def _resolve(env_key: str, cfg, section: str, cfg_key: str):
    """A platform setting as the host resolves it: env var wins, else the
    ``config.yaml`` platform-section key, else None. Checking both keeps us from
    warning about something the operator set in yaml instead of env."""
    v = os.environ.get(env_key)
    if v not in (None, ""):
        return v
    sec = (cfg or {}).get(section) or {}
    if isinstance(sec, dict) and sec.get(cfg_key) not in (None, ""):
        return str(sec.get(cfg_key))
    return None


def _platform_config_problems(cfg) -> list:
    """How each connected platform will behave given its auth/mention config —
    surfaced when the bot will answer nowhere you'd expect. Only for platforms
    actually in use (token/vars present), read from env AND config.yaml. Framed
    as behaviour, not error: the fully-open setup is a choice, so this informs.
    """
    env = os.environ
    out = []

    # ── Telegram (in use if a bot token is set) ──
    if env.get("TELEGRAM_BOT_TOKEN"):
        raw = _resolve("TELEGRAM_GROUP_ALLOWED_CHATS", cfg, "telegram", "group_allowed_chats") or ""
        chats = [c.strip() for c in raw.split(",") if c.strip()]
        users = _resolve("TELEGRAM_GROUP_ALLOWED_USERS", cfg, "telegram", "group_allowed_users")
        if not chats and not users:
            out.append("Telegram: no group authorized (TELEGRAM_GROUP_ALLOWED_CHATS empty) — the bot "
                       "IGNORES every group message. Add the group's chat id (negative) or '*'.")
        for c in chats:
            if c != "*" and not (c.startswith("-") and c[1:].isdigit()):
                out.append(f"Telegram: TELEGRAM_GROUP_ALLOWED_CHATS entry '{c}' is not a group id "
                           "(group ids are negative, e.g. -100…) — that entry authorizes nothing.")

    # ── Slack (in use if a token is set) ──
    if env.get("SLACK_BOT_TOKEN") or env.get("SLACK_APP_TOKEN"):
        if env.get("SLACK_ALLOW_ALL_USERS", "").lower() not in _TRUE and not env.get("SLACK_ALLOWED_USERS", "").strip():
            out.append("Slack: no user authorized — the bot IGNORES everyone. "
                       "Set SLACK_ALLOW_ALL_USERS=true, or list SLACK_ALLOWED_USERS.")
        mention = (_resolve("SLACK_REQUIRE_MENTION", cfg, "slack", "require_mention") or "").lower()
        free = _resolve("SLACK_FREE_RESPONSE_CHANNELS", cfg, "slack", "free_response_channels")
        if mention not in _FALSE and not free:
            out.append("Slack: the bot only replies when @mentioned in channels. Set "
                       "SLACK_REQUIRE_MENTION=false (or SLACK_FREE_RESPONSE_CHANNELS) to answer unmentioned messages.")

    # ── WhatsApp (in use if any WHATSAPP_* var is set) ──
    if any(k.startswith("WHATSAPP_") for k in env):
        policy = (_resolve("WHATSAPP_GROUP_POLICY", cfg, "whatsapp", "group_policy") or "").lower()
        if policy and policy not in {"open", "allowlist", "disabled", "pairing"}:
            out.append(f"WhatsApp: WHATSAPP_GROUP_POLICY='{policy}' is not valid "
                       "(open|allowlist|disabled|pairing) — likely a typo; groups may be blocked.")
        elif policy in ("", "pairing"):
            out.append("WhatsApp: group policy is 'pairing' (the default) — the bot will NOT answer in "
                       "groups until each is paired. Set WHATSAPP_GROUP_POLICY=open to answer in all groups.")
        wrm = (_resolve("WHATSAPP_REQUIRE_MENTION", cfg, "whatsapp", "require_mention") or "").lower()
        if wrm and wrm not in _FALSE:
            out.append("WhatsApp: WHATSAPP_REQUIRE_MENTION is on — the bot only replies when @mentioned.")

    return out


def _should_chat_warn() -> bool:
    """Chat-warn about config exactly ONCE ever (until the marker file is
    removed), then never again — a deliberate setup shouldn't be nagged for
    eternity. Only called when there ARE problems, so the marker is written the
    first time we'd actually warn. Logs still fire every boot regardless."""
    marker = Path.home() / ".hermes" / ".turn_taking_config_warned"
    if marker.exists():
        return False
    try:
        marker.write_text("1")
    except Exception:
        pass  # best-effort; worst case the warning repeats, never lost
    return True


def _warn_misconfig() -> None:
    """Warn about config that silently breaks turn-taking or makes the bot
    answer nowhere. Two families: plugin-core (URL/key/streaming/group_sessions
    — the plugin can't work) and per-platform auth/mention (it connects but
    ignores messages). Logged in full on every start; pushed to the home chat
    only the FIRST time (see _should_chat_warn), so a deliberate setup isn't
    nagged forever. URL-unset stays log-only (no inbound path to flush).
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
    problems += _platform_config_problems(cfg)
    for p in problems:
        _log.warning("turn-taking: %s", p)
    if problems and _should_chat_warn():
        notify.queue_startup("⚠️ turn-taking / platform config:\n• " + "\n• ".join(problems))


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
    try:
        social_learning.warm_recent_sessions()
    except Exception as e:
        _log.warning("turn-taking: social-learning warm-up skipped: %s", e)
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
