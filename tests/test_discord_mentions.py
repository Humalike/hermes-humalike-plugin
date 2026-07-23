"""Discord branch of `_annotate_mentions`: <@id>/<@!id> → @you / @DisplayName.

patching.py needs its package siblings, so we load state/notify/service for real
(httpx stubbed) and stub core (only names patching imports). Run directly:
python3 tests/test_discord_mentions.py  (or via pytest from inside tests/).
"""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _load_patching():
    sys.modules.setdefault("httpx", types.ModuleType("httpx"))
    httpx = sys.modules["httpx"]
    if not hasattr(httpx, "HTTPError"):
        httpx.HTTPStatusError = type("HTTPStatusError", (Exception,), {})
        httpx.HTTPError = type("HTTPError", (Exception,), {})

    pkg = types.ModuleType("dm_pkg")
    pkg.__path__ = [str(_ROOT)]
    sys.modules["dm_pkg"] = pkg
    tt = types.ModuleType("dm_pkg.turn_taking")
    tt.__path__ = [str(_ROOT / "turn_taking")]
    sys.modules["dm_pkg.turn_taking"] = tt

    def _mod(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        m = importlib.util.module_from_spec(spec)
        sys.modules[name] = m
        spec.loader.exec_module(m)
        return m

    _mod("dm_pkg._config", _ROOT / "_config.py")
    _mod("dm_pkg.turn_taking.state", _ROOT / "turn_taking" / "state.py")
    _mod("dm_pkg.turn_taking.notify", _ROOT / "turn_taking" / "notify.py")
    _mod("dm_pkg.turn_taking.service", _ROOT / "turn_taking" / "service.py")
    core = types.ModuleType("dm_pkg.turn_taking.core")
    for n in ("_inbound_gate", "_build_system_prompt_for_turn_taking", "_decide", "_delivery_meta"):
        setattr(core, n, lambda *a, **k: None)
    sys.modules["dm_pkg.turn_taking.core"] = core
    return _mod("dm_pkg.turn_taking.patching", _ROOT / "turn_taking" / "patching.py")


patching = _load_patching()


def _member(mid, display_name=None, name=None):
    return types.SimpleNamespace(id=mid, display_name=display_name, name=name)


def _adapter(bot_id=999):
    return types.SimpleNamespace(_client=types.SimpleNamespace(user=types.SimpleNamespace(id=bot_id)))


def _event(text, mentions):
    return types.SimpleNamespace(text=text, source=None, raw_message=types.SimpleNamespace(mentions=mentions))


def _annotate(adapter, event):
    asyncio.run(patching._annotate_mentions(adapter, event))


def test_self_mention_becomes_you():
    ev = _event("<@999> hello", [_member(999, "Bot")])
    _annotate(_adapter(), ev)
    assert ev._tt_content == "@you hello"


def test_other_member_gets_display_name_and_bang_variant():
    ev = _event("hey <@!42>", [_member(42, "Alice")])
    _annotate(_adapter(), ev)
    assert ev._tt_content == "hey @Alice"


def test_no_mentions_left_untouched():
    ev = _event("plain text", [])
    _annotate(_adapter(), ev)
    assert not hasattr(ev, "_tt_content")


if __name__ == "__main__":
    test_self_mention_becomes_you()
    test_other_member_gets_display_name_and_bang_variant()
    test_no_mentions_left_untouched()
    print("ok")
