"""Hermes turn-taking plugin — entry point and registration.

The implementation is split into three sibling packages this module wires
together:
  - ``turn_taking/``      the turn-taking machinery, itself split by concern:
      service (HTTP/WS I/O), state (shared runtime state), delivery (bubble
      delivery + thread lifecycle), core (decide/naturalize + system-prompt
      build), patching (the gateway monkeypatches), hooks (the plugin hooks)
  - ``social_learning/``  the per-conversation voice card (pre_llm_call hook)
  - ``soul/``             the SOUL.md persona feature (the /soul command)
  - ``native_memory``     optionally strips native memory's *style* capture so
                          the voice card owns style; off unless
                          ``native_memory.strip_style: true`` in config.yaml

This module only registers the plugin's command, hooks, and patches.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

from . import _config, autoconfig, connect, login, native_memory, social_learning, soul
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

_log = logging.getLogger(__name__)


# The host's truthy set for allow-all/enabled flags is exactly this — no "on"
# (slack/whatsapp adapters use `in {"true", "1", "yes"}`). _FALSE matches the
# host's require_mention parse (`not in {"false", "0", "no", "off"}`).
_HOST_TRUE = {"true", "1", "yes"}
_FALSE = {"false", "0", "no", "off"}


def _sections(cfg, name):
    """Every yaml spot the host reads a platform section from: top-level
    ``<name>:``, ``platforms.<name>:`` and ``gateway.platforms.<name>:``."""
    c = cfg if isinstance(cfg, dict) else {}
    for holder in (c, c.get("platforms"), (c.get("gateway") or {}).get("platforms")):
        sec = holder.get(name) if isinstance(holder, dict) else None
        if isinstance(sec, dict):
            yield sec


def _resolve(env_key: str, cfg, section: str, cfg_key: str):
    """A platform setting as the host resolves it: adapters read their yaml
    section (``config.extra``) before env, so yaml wins here too. Lists join
    to CSV exactly like the host's yaml→env bridges do."""
    for sec in _sections(cfg, section):
        v = sec.get(cfg_key)
        if v is None or v == "":
            continue
        if isinstance(v, list):
            return ",".join(str(x) for x in v)
        return str(v)
    v = os.environ.get(env_key)
    return v if v not in (None, "") else None


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
        if not chats:
            # User-level allowlists don't substitute: group OBSERVATION (what
            # turn-taking runs on) requires an explicit chat allowlist in the
            # host — with only user auth the bot answers direct @mentions but
            # never sees the group's conversation stream.
            out.append("Telegram: TELEGRAM_GROUP_ALLOWED_CHATS is empty — the bot cannot observe "
                       "group conversation, so group turn-taking is OFF (at most it answers direct "
                       "@mentions from allowed users). Add each group's chat id (negative, e.g. -100…).")
        for c in chats:
            if not (c.startswith("-") and c[1:].isdigit()):
                out.append(f"Telegram: TELEGRAM_GROUP_ALLOWED_CHATS entry '{c}' is not a group id — "
                           "Hermes matches chat ids literally ('*' does not work here; group ids are "
                           "negative, e.g. -100…), so that entry authorizes nothing.")

    # ── Slack (in use if a token is set; user auth is env-only in the host) ──
    if env.get("SLACK_BOT_TOKEN") or env.get("SLACK_APP_TOKEN"):
        allow_all = any(env.get(k, "").strip().lower() in _HOST_TRUE
                        for k in ("SLACK_ALLOW_ALL_USERS", "GATEWAY_ALLOW_ALL_USERS"))
        listed = env.get("SLACK_ALLOWED_USERS", "").strip() or env.get("GATEWAY_ALLOWED_USERS", "").strip()
        if not allow_all and not listed:
            out.append("Slack: no user authorized — the bot ignores everyone except already-paired "
                       "users. Set SLACK_ALLOW_ALL_USERS=true, or list SLACK_ALLOWED_USERS.")
        for k in ("SLACK_ALLOW_ALL_USERS", "GATEWAY_ALLOW_ALL_USERS"):
            v = env.get(k, "").strip().lower()
            if v and v not in _HOST_TRUE and v not in _FALSE:
                out.append(f"Slack: {k}='{v}' is not a value Hermes accepts (true/1/yes) — "
                           "it is treated as OFF.")
        mention = (_resolve("SLACK_REQUIRE_MENTION", cfg, "slack", "require_mention") or "").lower()
        free = _resolve("SLACK_FREE_RESPONSE_CHANNELS", cfg, "slack", "free_response_channels")
        if mention not in _FALSE and not free:
            out.append("Slack: the bot only replies when @mentioned in channels. Set "
                       "SLACK_REQUIRE_MENTION=false (or SLACK_FREE_RESPONSE_CHANNELS) to answer unmentioned messages.")
        rit = next((sec.get("reply_in_thread") for sec in _sections(cfg, "slack")
                    if "reply_in_thread" in sec), None)
        if rit is None or str(rit).strip().lower() not in _FALSE:
            out.append("Slack: reply_in_thread is not false — every top-level channel message "
                       "becomes its own thread AND its own session, so turn-taking answers each "
                       "message separately. Set 'slack:\n  reply_in_thread: false' in ~/.hermes/config.yaml.")

    # ── WhatsApp (in use only when the host would enable it: WHATSAPP_ENABLED
    # truthy — WHATSAPP_ENABLED=false or WHATSAPP_CLOUD_* alone don't count) ──
    wa_enabled = (_resolve("WHATSAPP_ENABLED", cfg, "whatsapp", "enabled") or "").strip().lower()
    if wa_enabled in _HOST_TRUE:
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


def _marker_path() -> Path:
    """Chat-warn marker in the real hermes home (HERMES_HOME-aware); falls
    back to ~/.hermes when hermes_constants isn't importable."""
    try:
        from hermes_constants import get_hermes_home  # noqa: PLC0415
        return Path(get_hermes_home()) / ".turn_taking_config_warned"
    except Exception:
        return Path.home() / ".hermes" / ".turn_taking_config_warned"


def _should_chat_warn(digest: str) -> bool:
    """Chat-warn once per distinct problem set: the marker holds the digest of
    the problems that were actually DELIVERED to a home chat (see
    _mark_chat_warned). The same set stays silent — a deliberate setup isn't
    nagged — but any new or changed problem warns again. Logs still fire every
    boot regardless."""
    try:
        return _marker_path().read_text().strip() != digest
    except Exception:
        return True


def _mark_chat_warned(digest: str) -> None:
    """Delivery callback from notify: record WHAT was warned about, only once
    the message really reached a home chat. Queued-but-undelivered warnings
    (URL unset, CLI process, no /sethome yet) keep re-queueing until seen."""
    try:
        p = _marker_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(digest)
    except Exception:
        pass  # best-effort; worst case the warning repeats, never lost


def _warn_misconfig() -> None:
    """Warn about config that silently breaks turn-taking or makes the bot
    answer nowhere. Two families: plugin-core (URL/key/streaming/group_sessions
    — the plugin can't work) and per-platform auth/mention (it connects but
    ignores messages). Logged in full on every start; pushed to the home chat
    once per distinct problem set, and only counted as warned when actually
    delivered (see _should_chat_warn/_mark_chat_warned). While URL is unset the
    push stays queued (no inbound path installs), so it arrives — not is lost —
    once the plugin is configured.
    """
    problems = []
    if not _config.service_url():
        problems.append("HUMALIKE_API_URL is set empty — turn-taking is explicitly "
                        "DISABLED (/soul still works).")
    elif not _config.api_key():
        problems.append("HUMALIKE_API_KEY is not set — every Humalike call will "
                        "fail with 401. Send the bot /connect to link your "
                        "Humalike account, or add the key to ~/.hermes/.env.")
    try:
        import yaml

        # Same HERMES_HOME-aware path autoconfig WRITES to — a hardcoded
        # ~/.hermes here reads a stray/empty file under a relocated home (Docker
        # HERMES_HOME=/opt/data, Windows), warning about config that is actually
        # correct on disk. login._hermes_home() is the host's own resolver.
        cfg = yaml.safe_load((login._hermes_home() / "config.yaml").read_text()) or {}
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
        display = cfg.get("display") if isinstance(cfg.get("display"), dict) else {}
        # Each check mirrors the gateway's own normalization of YAML quirks
        # (bare `off`/`no` parse as booleans; strings are lowercased), so we
        # only warn on configs the gateway would actually misread.
        tp = display.get("tool_progress")
        tp = "off" if tp is False else "all" if tp is True else str(tp).lower() if tp is not None else None
        if tp not in ("off", "log"):  # gateway: chatter posts unless mode ∈ {off, log}
            problems.append("display.tool_progress is not \"off\" — tool-call chatter "
                            "(Browsing/Clicking…) leaks into replies; set "
                            "'display.tool_progress: \"off\"' in ~/.hermes/config.yaml.")
        ack = display.get("busy_ack_enabled")
        if ack is None or str(ack).lower() == "true":  # gateway: enabled iff str(v) == "true"
            problems.append("display.busy_ack_enabled is not false — deterministic "
                            "'⚡ Interrupting current task…' acks are posted; set "
                            "'display.busy_ack_enabled: false' in ~/.hermes/config.yaml.")
        mem = display.get("memory_notifications")
        if not (mem is False or (isinstance(mem, str) and mem.lower() == "off")):
            problems.append("display.memory_notifications is not \"off\" — "
                            "'💾 Self-improvement review…' memory posts leak; set "
                            "'display.memory_notifications: \"off\"' in ~/.hermes/config.yaml.")
        if os.environ.get("TELEGRAM_BOT_TOKEN"):
            platforms = display.get("platforms") if isinstance(display.get("platforms"), dict) else {}
            telegram = platforms.get("telegram") if isinstance(platforms.get("telegram"), dict) else {}
            stream = telegram.get("streaming")
            stream = stream.lower() in ("true", "1", "yes", "on") if isinstance(stream, str) else stream
            if stream is None or bool(stream):  # absent, or normalizes truthy → streams
                problems.append("display.platforms.telegram.streaming is not false — the raw "
                                "draft streams before naturalization; set "
                                "'display.platforms.telegram.streaming: false' in "
                                "~/.hermes/config.yaml.")
        agent = cfg.get("agent") if isinstance(cfg.get("agent"), dict) else {}
        disabled = agent.get("disabled_toolsets")
        disabled = disabled if isinstance(disabled, list) else []
        if "clarify" not in disabled:
            problems.append("agent.disabled_toolsets does not include 'clarify' — the "
                            "clarify tool's numbered menus bypass naturalization; add "
                            "'clarify' to agent.disabled_toolsets in ~/.hermes/config.yaml.")
    problems += _platform_config_problems(cfg)
    for p in problems:
        _log.warning("turn-taking: %s", p)
    if problems:
        digest = hashlib.sha256("\n".join(sorted(problems)).encode()).hexdigest()
        if _should_chat_warn(digest):
            notify.queue_startup(
                "⚠️ turn-taking / platform config:\n• " + "\n• ".join(problems),
                on_delivered=lambda: _mark_chat_warned(digest),
            )


def register(ctx) -> None:
    """Plugin entry point: activate the send + inbound patches.

    Idle (no patching) only when turn-taking is explicitly disabled
    (``HUMALIKE_API_URL`` set EMPTY — the URL defaults to the public API
    otherwise) — the ``/soul`` persona command is registered regardless (it
    uses the separate Personas API, not the turn-taking service).
    """
    try:
        # First boot: apply the deterministic parts of the install/configure
        # skills (required config.yaml settings; respond-to-everyone env for
        # WhatsApp/Slack when in use; Telegram's manual steps prompted). Runs
        # BEFORE _warn_misconfig so the warnings reflect the fixed state.
        autoconfig.maybe_autoconfigure()
    except Exception as e:
        _log.warning("turn-taking: autoconfig skipped: %s", e)
    _warn_misconfig()
    try:
        # First keyless boot: pop the device-auth login once (browser tab on a
        # desktop, printed URL on the gateway console otherwise) so a fresh
        # install connects immediately. /connect and login.py are the retries.
        login.maybe_first_boot_login()
    except Exception as e:
        _log.warning("turn-taking: first-boot login skipped: %s", e)
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
        ctx.register_command(
            "connect", connect.command,
            description="Link this agent to your Humalike account (device login)",
        )
        _log.info("turn-taking: registered /connect command")
    except Exception as e:
        _log.warning("turn-taking: could not register /connect command: %s", e)
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
    # Off by default; opt in with ``native_memory.strip_style: true`` to stop
    # native memory from also capturing style (the voice card owns style).
    try:
        native_memory.strip_native_style_capture()
    except Exception as e:
        _log.warning("turn-taking: native-memory style strip skipped: %s", e)
    # Patches install unconditionally — every service call self-gates on the
    # key (service.py), so a keyless boot is inert and /connect activates
    # turn-taking live, no restart needed.
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
