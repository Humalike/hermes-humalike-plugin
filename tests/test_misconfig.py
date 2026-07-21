"""Checks for the platform-config warning logic in the plugin root module.

The root __init__.py wires the whole plugin (patching, soul, connect…), so
every sibling except _config is stubbed and only _resolve,
_platform_config_problems and the chat-warn marker digest logic are exercised.
Run directly:  python3 tests/test_misconfig.py
"""

import importlib.util
import os
import sys
import types
from pathlib import Path
from unittest.mock import patch

import yaml

_ROOT = Path(__file__).resolve().parent.parent


def _load():
    sys.modules.setdefault("httpx", types.ModuleType("httpx"))

    pkg = types.ModuleType("_tt_test_pkg")
    pkg.__path__ = [str(_ROOT)]
    sys.modules["_tt_test_pkg"] = pkg

    def _stub(name, **attrs):
        m = types.ModuleType(f"_tt_test_pkg.{name}")
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[f"_tt_test_pkg.{name}"] = m
        return m

    def noop(*a, **k):
        return None

    _stub("connect", command=noop)
    _stub("social_learning", on_pre_llm_call=noop, warm_recent_sessions=noop)
    _stub("soul", command=noop, maybe_auto_enhance=noop)
    tt = _stub("turn_taking")
    tt.__path__ = []
    tt.hooks = _stub("turn_taking.hooks", on_transform_llm_output=noop)
    tt.patching = _stub("turn_taking.patching", **{n: (lambda: False) for n in (
        "_patch__enqueue_text_event", "_patch_merge_pending_message_event",
        "_patch__poll_messages", "_patch_send", "_patch_slack_handle_message",
        "_patch_telegram_handle_message", "_patch_telegram_observe_group")})
    tt.notify = _stub("turn_taking.notify", queue_startup=noop)
    tt.service = _stub("turn_taking.service", _service_url=lambda: "")

    cfg_spec = importlib.util.spec_from_file_location("_tt_test_pkg._config", _ROOT / "_config.py")
    cfg = importlib.util.module_from_spec(cfg_spec)
    sys.modules["_tt_test_pkg._config"] = cfg
    cfg_spec.loader.exec_module(cfg)

    spec = importlib.util.spec_from_file_location("_tt_test_pkg.plugin_root", _ROOT / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_tt_test_pkg.plugin_root"] = mod
    spec.loader.exec_module(mod)
    return mod


_MOD = _load()


def test_resolve_yaml_first_lists_and_nesting():
    """yaml wins over env (adapters read config.extra first), lists join to
    CSV, and platforms.<name>/gateway.platforms.<name> nesting is found."""
    cfg = {"telegram": {"group_allowed_chats": [-100123, -100456]}}
    with patch.dict(os.environ, {"TELEGRAM_GROUP_ALLOWED_CHATS": "-1"}, clear=True):
        got = _MOD._resolve("TELEGRAM_GROUP_ALLOWED_CHATS", cfg, "telegram", "group_allowed_chats")
    assert got == "-100123,-100456", got

    nested = {"platforms": {"slack": {"require_mention": False}}}
    with patch.dict(os.environ, {}, clear=True):
        assert _MOD._resolve("SLACK_REQUIRE_MENTION", nested, "slack", "require_mention") == "False"
        assert _MOD._resolve("SLACK_REQUIRE_MENTION", None, "slack", "require_mention") is None
        deep = {"gateway": {"platforms": {"whatsapp": {"group_policy": "open"}}}}
        assert _MOD._resolve("WHATSAPP_GROUP_POLICY", deep, "whatsapp", "group_policy") == "open"


def test_platform_problems_telegram():
    # Valid yaml list -> no warnings at all.
    with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t"}, clear=True):
        assert _MOD._platform_config_problems({"telegram": {"group_allowed_chats": [-100123]}}) == []
    # User-level auth alone does NOT enable group observation -> still warns.
    with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_ALLOWED_USERS": "42",
                                 "TELEGRAM_GROUP_ALLOWED_USERS": "42"}, clear=True):
        probs = _MOD._platform_config_problems({})
        assert len(probs) == 1 and "cannot observe" in probs[0], probs
    # '*' is matched literally by the host -> flagged as authorizing nothing.
    with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_GROUP_ALLOWED_CHATS": "*"}, clear=True):
        assert any("authorizes nothing" in p for p in _MOD._platform_config_problems({}))


def test_platform_problems_slack():
    # 'on' is not host-truthy: flagged as unrecognized AND nobody is authorized.
    with patch.dict(os.environ, {"SLACK_BOT_TOKEN": "t", "SLACK_ALLOW_ALL_USERS": "on",
                                 "SLACK_REQUIRE_MENTION": "false"}, clear=True):
        probs = _MOD._platform_config_problems({})
        assert any("not a value Hermes accepts" in p for p in probs), probs
        assert any("no user authorized" in p for p in probs), probs
        assert any("reply_in_thread" in p for p in probs), probs  # not set -> warned
    # GATEWAY_ALLOWED_USERS counts as authorization (host honors it); with
    # reply_in_thread: false in yaml there is nothing left to warn about.
    with patch.dict(os.environ, {"SLACK_BOT_TOKEN": "t", "GATEWAY_ALLOWED_USERS": "U1",
                                 "SLACK_REQUIRE_MENTION": "false"}, clear=True):
        assert _MOD._platform_config_problems({"slack": {"reply_in_thread": False}}) == []


def test_platform_problems_whatsapp():
    # Explicitly disabled (or cloud-only vars) -> platform not in use, no warnings.
    with patch.dict(os.environ, {"WHATSAPP_ENABLED": "false", "WHATSAPP_CLOUD_ACCESS_TOKEN": "x"}, clear=True):
        assert _MOD._platform_config_problems({}) == []
    # Enabled with default policy -> pairing warning.
    with patch.dict(os.environ, {"WHATSAPP_ENABLED": "true"}, clear=True):
        assert any("pairing" in p for p in _MOD._platform_config_problems({}))


_CLEAN_CORE_CFG = {
    "streaming": False,
    "group_sessions_per_user": False,
    "display": {
        "tool_progress": "off",
        "busy_ack_enabled": False,
        "memory_notifications": "off",
        "platforms": {"telegram": {"streaming": False}},
    },
    "agent": {"disabled_toolsets": ["clarify"]},
}
_CLEAN_ENV = {"TELEGRAM_BOT_TOKEN": "t", "HUMALIKE_API_KEY": "k"}


def test_warn_misconfig_core_display_and_agent_checks():
    """The five autoconfigured 'core' settings _warn_misconfig now verifies:
    clean cfg -> no problems; each one missing/wrong -> its own message."""
    with patch.dict(os.environ, _CLEAN_ENV, clear=True):
        probs = _extract_core_problems(_CLEAN_CORE_CFG)
    assert probs == [], probs

    with patch.dict(os.environ, _CLEAN_ENV, clear=True):
        probs = _extract_core_problems({**_CLEAN_CORE_CFG, "display": {}, "agent": {}})
    assert any("tool_progress" in p for p in probs), probs
    assert any("busy_ack_enabled" in p for p in probs), probs
    assert any("memory_notifications" in p for p in probs), probs
    assert any("platforms.telegram.streaming" in p for p in probs), probs
    assert any("disabled_toolsets" in p for p in probs), probs

    # Spellings the gateway normalizes to the same behavior are compliant:
    # bare YAML `off` (-> False), case variants, stringy booleans.
    lenient = {
        "tool_progress": False,            # gateway: False -> "off"
        "busy_ack_enabled": "false",       # gateway: enabled iff str(v)=="true"
        "memory_notifications": False,     # gateway: bool False -> "off"
        "platforms": {"telegram": {"streaming": "no"}},  # gateway: str -> bool
    }
    with patch.dict(os.environ, _CLEAN_ENV, clear=True):
        probs = _extract_core_problems({**_CLEAN_CORE_CFG, "display": lenient})
    assert probs == [], probs

    # Truthy-normalizing spellings still warn.
    noisy = {
        "tool_progress": "all",
        "busy_ack_enabled": "TRUE",
        "memory_notifications": "verbose",
        "platforms": {"telegram": {"streaming": "yes"}},
    }
    with patch.dict(os.environ, _CLEAN_ENV, clear=True):
        probs = _extract_core_problems({**_CLEAN_CORE_CFG, "display": noisy})
    for key in ("tool_progress", "busy_ack_enabled", "memory_notifications", "telegram.streaming"):
        assert any(key in p for p in probs), (key, probs)

    # Telegram streaming only warned when a Telegram bot token is in use.
    with patch.dict(os.environ, {"HUMALIKE_API_KEY": "k"}, clear=True):
        cfg = {**_CLEAN_CORE_CFG, "display": {**_CLEAN_CORE_CFG["display"], "platforms": {}}}
        probs = _extract_core_problems(cfg)
    assert not any("telegram.streaming" in p for p in probs), probs


def _extract_core_problems(cfg):
    """Run _warn_misconfig with the config-file read and platform/chat-warn
    side effects stubbed out, capturing the resulting problems list."""
    captured = []
    with patch.object(_MOD, "_platform_config_problems", return_value=[]), \
         patch.object(_MOD, "_should_chat_warn", return_value=False), \
         patch.object(_MOD.login, "_hermes_home", return_value=Path("/nonexistent")), \
         patch("pathlib.Path.read_text", return_value=yaml.dump(cfg)), \
         patch.object(_MOD._log, "warning", side_effect=lambda fmt, p: captured.append(p)):
        _MOD._warn_misconfig()
    return captured


def test_chat_warn_digest_marker():
    """Marker written only via _mark_chat_warned (delivery), keyed by problem
    digest: same set silent, changed set warns again; dir auto-created."""
    import tempfile

    home = Path(tempfile.mkdtemp()) / "hermes-home"  # does not exist yet
    fake_hc = types.ModuleType("hermes_constants")
    fake_hc.get_hermes_home = lambda: home
    sys.modules["hermes_constants"] = fake_hc
    try:
        assert _MOD._should_chat_warn("d1") is True
        _MOD._mark_chat_warned("d1")
        assert (home / ".turn_taking_config_warned").exists()
        assert _MOD._should_chat_warn("d1") is False
        assert _MOD._should_chat_warn("d2") is True
    finally:
        sys.modules.pop("hermes_constants", None)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all tests passed")
