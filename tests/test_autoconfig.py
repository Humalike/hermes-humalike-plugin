"""Checks for the first-boot self-configuration planner.

autoconfig.py's plan() is pure (dicts in, updates out), so no yaml/network is
needed. Loaded under a fake parent package (the social_learning loader). Run
directly:  python3 tests/test_autoconfig.py
"""

import importlib.util
import sys
import tempfile
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _load():
    pkg = types.ModuleType("_humalike_test_pkg2")
    pkg.__path__ = [str(_ROOT)]
    sys.modules["_humalike_test_pkg2"] = pkg

    def _mod(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        mod.__package__ = "_humalike_test_pkg2"
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    lg = _mod("_humalike_test_pkg2.login", _ROOT / "login.py")
    ac = _mod("_humalike_test_pkg2.autoconfig", _ROOT / "autoconfig.py")
    return ac, lg


autoconfig, login = _load()
login.HERMES_ENV = Path(tempfile.mkdtemp()) / ".env"  # never read the real one

_COMPLIANT = {"streaming": False, "group_sessions_per_user": False,
              "display": {"tool_progress": "off"}}


def _plan(cfg=None, env=None, done=None):
    return autoconfig.plan(cfg or {}, env or {}, done or set())


# ── Core config ───────────────────────────────────────────────────────────────
def test_fresh_install_fixes_all_core_settings():
    config_updates, env_updates, statuses, todos, sections = _plan()
    assert config_updates == {"streaming": False, "group_sessions_per_user": False,
                              "display": {"tool_progress": "off"}}, config_updates
    assert not env_updates and not todos
    assert sections == ["core"]
    assert len(statuses) == 3 and all(k == "fixed" for k, _ in statuses)
    assert "streaming: (unset) → false" in statuses[0][1]  # old → new shown


def test_compliant_config_verifies_core_without_changes():
    config_updates, _, statuses, _, sections = _plan(cfg=dict(_COMPLIANT))
    assert not config_updates
    assert statuses == [("ok", "chat settings")]  # granular ✓, not silence
    assert sections == ["core"]  # recorded so it isn't re-checked every boot


def test_core_done_is_skipped_and_display_merge_preserves_keys():
    config_updates, _, statuses, _, sections = _plan(cfg=dict(_COMPLIANT), done={"core"})
    assert not config_updates and not statuses and not sections
    config_updates, _, _, _, _ = _plan(cfg={"display": {"theme": "x"}})
    assert config_updates["display"] == {"theme": "x", "tool_progress": "off"}


# ── Platforms ─────────────────────────────────────────────────────────────────
def test_whatsapp_fills_only_unset_keys():
    _, env_updates, statuses, _, sections = _plan(
        cfg=dict(_COMPLIANT), done={"core"},
        env={"WHATSAPP_ENABLED": "true", "WHATSAPP_GROUP_POLICY": "allowlist"})
    assert env_updates == {"WHATSAPP_ALLOW_ALL_USERS": "true",
                           "WHATSAPP_REQUIRE_MENTION": "false"}, env_updates
    assert "whatsapp" in sections
    fixed = [t for k, t in statuses if k == "fixed"]
    assert any(t.startswith("WHATSAPP_ALLOW_ALL_USERS: (unset) → true") for t in fixed), fixed
    assert any(t.startswith("WHATSAPP_REQUIRE_MENTION: (unset) → false") for t in fixed), fixed
    assert not any("GROUP_POLICY" in t for t in fixed)  # preset key untouched


def test_whatsapp_already_right_shows_verified():
    _, env_updates, statuses, _, _ = _plan(done={"core"}, env={
        "WHATSAPP_ENABLED": "true", "WHATSAPP_ALLOW_ALL_USERS": "true",
        "WHATSAPP_REQUIRE_MENTION": "false", "WHATSAPP_GROUP_POLICY": "open"})
    assert not env_updates
    assert ("ok", "WhatsApp") in statuses


def test_whatsapp_not_enabled_or_done_is_skipped():
    _, env_updates, _, _, sections = _plan(env={"WHATSAPP_ENABLED": "false"}, done={"core"})
    assert not env_updates and not sections
    _, env_updates, _, _, sections = _plan(
        env={"WHATSAPP_ENABLED": "true"}, done={"core", "whatsapp"})
    assert not env_updates and not sections


def test_slack_fills_only_unset_keys_and_forces_reply_in_thread_off():
    config_updates, env_updates, statuses, _, sections = _plan(
        done={"core"},
        env={"SLACK_APP_TOKEN": "xapp-1", "SLACK_ALLOW_ALL_USERS": "true"})
    assert env_updates == {"SLACK_REQUIRE_MENTION": "false"}, env_updates
    assert config_updates == {"slack": {"reply_in_thread": False}}, config_updates
    assert "slack" in sections
    assert statuses and statuses[-1][0] == "fixed"


def test_slack_already_right_shows_verified_and_merge_preserves_keys():
    config_updates, _, statuses, _, _ = _plan(
        done={"core"}, cfg={"slack": {"reply_in_thread": False}},
        env={"SLACK_BOT_TOKEN": "xoxb-1", "SLACK_ALLOW_ALL_USERS": "true",
             "SLACK_REQUIRE_MENTION": "false"})
    assert "slack" not in config_updates
    assert ("ok", "Slack") in statuses
    config_updates, _, _, _, _ = _plan(
        done={"core"}, cfg={"slack": {"require_mention": False}},
        env={"SLACK_BOT_TOKEN": "xoxb-1"})
    assert config_updates["slack"] == {"require_mention": False, "reply_in_thread": False}


def test_telegram_is_prompt_only():
    _, env_updates, statuses, todos, sections = _plan(
        done={"core"}, env={"TELEGRAM_BOT_TOKEN": "123:abc"})
    assert not env_updates  # never guesses chat ids
    assert "telegram" in sections
    assert todos and "BotFather" in todos[0]
    # chats already configured → verified, nothing to prompt
    _, _, statuses, todos, _ = _plan(done={"core"}, env={
        "TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_GROUP_ALLOWED_CHATS": "-100123"})
    assert not todos
    assert ("ok", "Telegram") in statuses


# ── upsert_env (the generic writer autoconfig relies on) ─────────────────────
def test_upsert_env_updates_many_and_preserves_comments():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / ".env"
        p.write_text("# keep me\nTELEGRAM_BOT_TOKEN=t\nSLACK_REQUIRE_MENTION=true\n")
        login.upsert_env(p, {"SLACK_REQUIRE_MENTION": "false", "SLACK_ALLOW_ALL_USERS": "true"})
        lines = p.read_text().splitlines()
        assert "# keep me" in lines and "TELEGRAM_BOT_TOKEN=t" in lines
        assert "SLACK_REQUIRE_MENTION=false" in lines
        assert "SLACK_ALLOW_ALL_USERS=true" in lines
        assert "SLACK_REQUIRE_MENTION=true" not in lines


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all passed")
