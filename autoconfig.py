"""First-boot self-configuration — hermes configures itself for turn-taking.

Applies the deterministic parts of the install/configure skills so a fresh
install needs no hand-editing (the same zero-config principle as login.py):

* ``config.yaml`` — the settings the plugin REQUIRES to own the reply:
  ``streaming: false``, ``group_sessions_per_user: false``,
  ``display.tool_progress: "off"`` (install-turn-taking step 3), and
  ``slack.reply_in_thread: false`` when Slack is in use (the default trues it,
  making every top-level channel message its own thread AND session).
* ``.env`` — respond-to-everyone settings for platforms actually in use, only
  where the operator hasn't set a value: WhatsApp (configure-whatsapp-group)
  and Slack (configure-slack-group).
* Telegram can't be automated — BotFather's privacy toggle is external and
  group chat ids are unknowable — so those two steps are prompted instead
  (configure-telegram-group).

Each section applies ONCE (the marker file lists finished sections), so an
operator who later overrides a value isn't fought every boot — but adding a
new platform later still triggers just that platform's section. The report is
granular and TUI-safe (login._show): every swept section shows its status —
``✓`` already right, ``•`` fixed, ``📋`` manual steps — so the operator always
sees what was checked, not just what changed. Normal boots (marker complete)
stay silent. config.yaml is rewritten via yaml round-trip; comments there are
lost — keys and values are preserved.
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


def _was(value) -> str:
    """The previous value, human-readable: ``(unset)`` / yaml-style booleans."""
    if value is None or value == "":
        return "(unset)"
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def plan(cfg: dict, env: dict, done: set) -> tuple[dict, dict, list, list, list]:
    """Decide what's left to configure. Pure — takes the current config.yaml
    dict, a merged env view, and the already-done sections; returns
    ``(config_updates, env_updates, statuses, todos, sections)`` where
    ``statuses`` is ``[("fixed"|"ok", label), …]`` — one entry per swept
    section, so the report can show verified items, not just changed ones."""
    config_updates: dict = {}
    env_updates: dict = {}
    statuses: list = []
    todos: list = []
    sections: list = []

    # ── Core chat settings (required — fixed even if set the wrong way) ──
    if "core" not in done:
        fixes = []
        if cfg.get("streaming") is not False:
            config_updates["streaming"] = False
            fixes.append(f"streaming: {_was(cfg.get('streaming'))} → false — the plugin must "
                         "own the final reply text (config.yaml)")
        if cfg.get("group_sessions_per_user") is not False:
            config_updates["group_sessions_per_user"] = False
            fixes.append(f"group_sessions_per_user: {_was(cfg.get('group_sessions_per_user'))} "
                         "→ false — a group chat needs ONE shared conversation, not one per "
                         "member (config.yaml)")
        display = cfg.get("display") if isinstance(cfg.get("display"), dict) else {}
        if display.get("tool_progress") != "off":
            config_updates["display"] = {**display, "tool_progress": "off"}
            fixes.append(f"display.tool_progress: {_was(display.get('tool_progress'))} → off — "
                         "hide tool-call chatter (Browsing/Clicking…) so replies read as human "
                         "(config.yaml)")
        sections.append("core")
        statuses.extend(("fixed", f) for f in fixes)
        if not fixes:
            statuses.append(("ok", "chat settings"))

    # ── WhatsApp: respond to everyone (only fills keys the operator left unset) ──
    if "whatsapp" not in done and (env.get("WHATSAPP_ENABLED") or "").lower() in _TRUE:
        wa_fixes = 0
        for key, value, why in (
            ("WHATSAPP_ALLOW_ALL_USERS", "true", "reply to any sender, not only paired users"),
            ("WHATSAPP_REQUIRE_MENTION", "false", "reply without needing an @mention"),
            ("WHATSAPP_GROUP_POLICY", "open", "answer in every group — the default "
                                              "'pairing' blocks groups"),
        ):
            if not env.get(key):
                env_updates[key] = value
                statuses.append(("fixed", f"{key}: {_was(env.get(key))} → {value} — {why} (.env)"))
                wa_fixes += 1
        sections.append("whatsapp")
        if not wa_fixes:
            statuses.append(("ok", "WhatsApp"))

    # ── Slack: respond to everyone (same only-if-unset rule), plus the one
    # config.yaml key turn-taking REQUIRES there: reply_in_thread: false —
    # the default trues it, making every top-level channel message its own
    # thread AND session, so turn-taking can never coalesce a conversation. ──
    if "slack" not in done and (env.get("SLACK_BOT_TOKEN") or env.get("SLACK_APP_TOKEN")):
        slack_fixes = 0
        for key, value, why in (
            ("SLACK_ALLOW_ALL_USERS", "true", "reply to anyone in the workspace"),
            ("SLACK_REQUIRE_MENTION", "false", "see and answer unmentioned channel messages"),
        ):
            if not env.get(key):
                env_updates[key] = value
                statuses.append(("fixed", f"{key}: {_was(env.get(key))} → {value} — {why} (.env)"))
                slack_fixes += 1
        slack_cfg = cfg.get("slack") if isinstance(cfg.get("slack"), dict) else {}
        if slack_cfg.get("reply_in_thread") is not False:
            config_updates["slack"] = {**slack_cfg, "reply_in_thread": False}
            statuses.append(("fixed", "slack.reply_in_thread: "
                                      f"{_was(slack_cfg.get('reply_in_thread'))} → false — one "
                                      "shared conversation per channel; the default makes EVERY "
                                      "message its own thread and session, so the bot answers "
                                      "each one separately (config.yaml)"))
            slack_fixes += 1
        sections.append("slack")
        if not slack_fixes:
            statuses.append(("ok", "Slack"))

    # ── Telegram: prompt-only (once) ──
    if "telegram" not in done and env.get("TELEGRAM_BOT_TOKEN"):
        sections.append("telegram")
        if not env.get("TELEGRAM_GROUP_ALLOWED_CHATS"):
            todos.append(_TELEGRAM_TODO)
        else:
            statuses.append(("ok", "Telegram"))

    return config_updates, env_updates, statuses, todos, sections


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

    config_updates, env_updates, statuses, todos, sections = plan(cfg, _merged_env(), done)
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
            sections = [s for s in sections if s != "core"]  # retry next boot
            statuses = [s for s in statuses
                        if not s[1].startswith(("streaming:", "group_sessions_per_user:",
                                                "display.", "slack.reply_in_thread:"))]
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

    for kind, label in statuses:
        if kind == "fixed":
            _log.warning("turn-taking: autoconfigured %s", label)
        else:
            _log.info("turn-taking: autoconfig verified %s — already right", label)
    if not statuses and not todos:
        return

    any_fixed = any(kind == "fixed" for kind, _ in statuses)
    header = ("🔧 Humalike plugin — configured hermes for turn-taking:" if any_fixed
              else "✅ Humalike plugin — turn-taking setup verified:")
    lines = [f"   {'•' if kind == 'fixed' else '✓'} {label}"
             + ("" if kind == "fixed" else " — already right")
             for kind, label in statuses]
    if any_fixed:
        lines.append("   Restart hermes once to apply.")
    parts = ["\n".join([header] + lines)] if statuses else []
    parts.extend(todos)
    report = "\n\n" + "\n\n".join(parts) + "\n"

    def _announce() -> None:
        login._wait_for_tui()
        login._show(report)

    threading.Thread(target=_announce, daemon=True, name="humalike-autoconfig").start()
