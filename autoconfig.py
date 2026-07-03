"""First-boot self-configuration — hermes configures itself for turn-taking.

Applies the deterministic parts of the install/configure skills so a fresh
install needs no hand-editing (the same zero-config principle as login.py):

* ``~/.hermes/config.yaml`` — the settings the plugin REQUIRES to own the
  reply: ``streaming: false``, ``group_sessions_per_user: false``,
  ``display.tool_progress: "off"`` (install-turn-taking step 3).
* ``~/.hermes/.env`` — respond-to-everyone settings for platforms actually in
  use, only where the operator hasn't set a value: WhatsApp
  (configure-whatsapp-group) and Slack (configure-slack-group).
* Telegram can't be automated — BotFather's privacy toggle is external and
  group chat ids are unknowable — so those two steps are prompted instead
  (configure-telegram-group).

Each section applies ONCE (the marker file lists finished sections), so an
operator who later overrides a value isn't fought every boot — but adding a
new platform later still triggers just that platform's section. The report is
shown TUI-safely via login._show once the TUI is up. config.yaml is rewritten
via yaml round-trip; comments there are lost — keys and values are preserved.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from . import login

_log = logging.getLogger(__name__)

# Resolved through the host's own HERMES_HOME-aware resolver (login._hermes_home)
# so a relocated install gets its REAL files updated, not a stray ~/.hermes.
_MARKER = login._hermes_home() / ".turn_taking_autoconfigured"
_CONFIG = login._hermes_home() / "config.yaml"

_TRUE = {"true", "1", "yes"}  # the host's truthy set for enable flags

# Every env key the planner reads or writes (env wins over ~/.hermes/.env).
_ENV_KEYS = (
    "WHATSAPP_ENABLED", "WHATSAPP_ALLOW_ALL_USERS", "WHATSAPP_REQUIRE_MENTION",
    "WHATSAPP_GROUP_POLICY",
    "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_ALLOW_ALL_USERS", "SLACK_REQUIRE_MENTION",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_GROUP_ALLOWED_CHATS",
)

_TELEGRAM_TODO = (
    "📋 Telegram needs two manual steps (they can't be automated):\n"
    "   1. @BotFather → /setprivacy → Disable — then remove the bot from the\n"
    "      group and re-add it (the change only applies to a fresh membership).\n"
    "   2. Add the group's chat id to TELEGRAM_GROUP_ALLOWED_CHATS in\n"
    "      ~/.hermes/.env (find it in the logs: \"tt inbound: chat=-100…\";\n"
    "      each group has its own id)."
)


def plan(cfg: dict, env: dict, done: set) -> tuple[dict, dict, list, list, list]:
    """Decide what's left to configure. Pure — takes the current config.yaml
    dict, a merged env view, and the already-done sections; returns
    ``(config_updates, env_updates, notes, todos, sections)``."""
    config_updates: dict = {}
    env_updates: dict = {}
    notes: list = []
    todos: list = []
    sections: list = []

    # ── Core chat settings (required — fixed even if set the wrong way) ──
    if "core" not in done:
        if cfg.get("streaming") is not False:
            config_updates["streaming"] = False
        if cfg.get("group_sessions_per_user") is not False:
            config_updates["group_sessions_per_user"] = False
        display = cfg.get("display") if isinstance(cfg.get("display"), dict) else {}
        if display.get("tool_progress") != "off":
            config_updates["display"] = {**display, "tool_progress": "off"}
        sections.append("core")
        if config_updates:
            notes.append("core chat settings — streaming off, one shared thread per "
                         "group, tool-call chatter hidden")

    # ── WhatsApp: respond to everyone (only fills keys the operator left unset) ──
    if "whatsapp" not in done and (env.get("WHATSAPP_ENABLED") or "").lower() in _TRUE:
        for key, value in (("WHATSAPP_ALLOW_ALL_USERS", "true"),
                           ("WHATSAPP_REQUIRE_MENTION", "false"),
                           ("WHATSAPP_GROUP_POLICY", "open")):
            if not env.get(key):
                env_updates[key] = value
        sections.append("whatsapp")
        if any(k.startswith("WHATSAPP") for k in env_updates):
            notes.append("WhatsApp — respond to everyone, in every group, no @mention")

    # ── Slack: respond to everyone (same only-if-unset rule), plus the one
    # config.yaml key turn-taking REQUIRES there: reply_in_thread: false —
    # the default trues it, making every top-level channel message its own
    # thread AND session, so turn-taking can never coalesce a conversation. ──
    if "slack" not in done and (env.get("SLACK_BOT_TOKEN") or env.get("SLACK_APP_TOKEN")):
        for key, value in (("SLACK_ALLOW_ALL_USERS", "true"),
                           ("SLACK_REQUIRE_MENTION", "false")):
            if not env.get(key):
                env_updates[key] = value
        slack_cfg = cfg.get("slack") if isinstance(cfg.get("slack"), dict) else {}
        if slack_cfg.get("reply_in_thread") is not False:
            config_updates["slack"] = {**slack_cfg, "reply_in_thread": False}
        sections.append("slack")
        if any(k.startswith("SLACK") for k in env_updates) or "slack" in config_updates:
            notes.append("Slack — respond to everyone, no @mention, one shared "
                         "conversation per channel (reply_in_thread off)")

    # ── Telegram: prompt-only (once) ──
    if "telegram" not in done and env.get("TELEGRAM_BOT_TOKEN"):
        sections.append("telegram")
        if not env.get("TELEGRAM_GROUP_ALLOWED_CHATS"):
            todos.append(_TELEGRAM_TODO)

    return config_updates, env_updates, notes, todos, sections


def _merged_env() -> dict:
    file_env = login.read_env_file()
    return {k: os.getenv(k) or file_env.get(k, "") for k in _ENV_KEYS}


def maybe_autoconfigure() -> None:
    """Apply whatever plan() says is left, record it, and report TUI-safely."""
    try:
        done = set(_MARKER.read_text().split()) if _MARKER.exists() else set()
    except Exception:
        done = set()

    cfg: dict = {}
    cfg_writable = True
    try:
        import yaml

        cfg = yaml.safe_load(_CONFIG.read_text()) or {}
        if not isinstance(cfg, dict):
            cfg, cfg_writable = {}, False
    except FileNotFoundError:
        pass  # fresh install — we create it
    except Exception as e:
        # Unparseable/unreadable: never risk clobbering the operator's file.
        _log.warning("turn-taking: autoconfig cannot read %s (%s) — config part skipped", _CONFIG, e)
        cfg_writable = False

    config_updates, env_updates, notes, todos, sections = plan(cfg, _merged_env(), done)
    if not sections:
        return

    if config_updates and cfg_writable:
        try:
            import yaml

            cfg.update(config_updates)
            _CONFIG.parent.mkdir(parents=True, exist_ok=True)
            _CONFIG.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))
        except Exception as e:
            _log.warning("turn-taking: autoconfig could not write %s: %s", _CONFIG, e)
            notes = [n for n in notes if not n.startswith("core chat settings")]
            sections = [s for s in sections if s != "core"]  # retry next boot
    if env_updates:
        try:
            login.upsert_env(login.HERMES_ENV, env_updates)
            os.environ.update(env_updates)  # keep this process consistent too
        except Exception as e:
            _log.warning("turn-taking: autoconfig could not write %s: %s", login.HERMES_ENV, e)

    try:
        _MARKER.parent.mkdir(parents=True, exist_ok=True)
        _MARKER.write_text("\n".join(sorted(done | set(sections))) + "\n")
    except Exception:
        pass  # best-effort; worst case a section re-applies (it is idempotent)

    for note in notes:
        _log.warning("turn-taking: autoconfigured %s", note)

    if notes or todos:
        parts = []
        if notes:
            parts.append("🔧 Humalike plugin — configured hermes for turn-taking:\n"
                         + "\n".join(f"   • {n}" for n in notes)
                         + "\n   Restart hermes once to apply.")
        parts.extend(todos)
        report = "\n\n" + "\n\n".join(parts) + "\n"
    else:
        # Nothing needed changing — still confirm ONCE (a sweep only runs when
        # the marker was missing): a fresh or re-armed install should get
        # positive confirmation, not silence that reads as failure.
        names = {"core": "chat settings", "whatsapp": "WhatsApp", "slack": "Slack",
                 "telegram": "Telegram"}
        checked = ", ".join(names.get(s, s) for s in sections)
        report = (f"\n✅ Humalike plugin — hermes is already configured for "
                  f"turn-taking (checked: {checked}).\n")
        _log.info("turn-taking: autoconfig verified — nothing to change (%s)", checked)

    def _announce() -> None:
        login._wait_for_tui()
        login._show(report)

    threading.Thread(target=_announce, daemon=True, name="humalike-autoconfig").start()
