"""Discord host-typing mute gate: `_discord_typing_muted` env parsing.

patching.py needs its package siblings, so we load state/notify/service for real
(httpx stubbed) and stub core (only names patching imports). Run directly:
python3 tests/test_discord_typing_mute.py  (or via pytest from inside tests/).
"""

import importlib.util
import os
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

    pkg = types.ModuleType("ttm_pkg")
    pkg.__path__ = [str(_ROOT)]
    sys.modules["ttm_pkg"] = pkg
    tt = types.ModuleType("ttm_pkg.turn_taking")
    tt.__path__ = [str(_ROOT / "turn_taking")]
    sys.modules["ttm_pkg.turn_taking"] = tt

    def _mod(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        m = importlib.util.module_from_spec(spec)
        sys.modules[name] = m
        spec.loader.exec_module(m)
        return m

    _mod("ttm_pkg._config", _ROOT / "_config.py")
    _mod("ttm_pkg.turn_taking.state", _ROOT / "turn_taking" / "state.py")
    _mod("ttm_pkg.turn_taking.notify", _ROOT / "turn_taking" / "notify.py")
    _mod("ttm_pkg.turn_taking.service", _ROOT / "turn_taking" / "service.py")
    core = types.ModuleType("ttm_pkg.turn_taking.core")
    for n in ("_inbound_gate", "_build_system_prompt_for_turn_taking", "_decide", "_delivery_meta"):
        setattr(core, n, lambda *a, **k: None)
    sys.modules["ttm_pkg.turn_taking.core"] = core
    return _mod("ttm_pkg.turn_taking.patching", _ROOT / "turn_taking" / "patching.py")


patching = _load_patching()


def test_discord_typing_muted_default_on_and_disableable():
    os.environ.pop("HERMES_DISCORD_MUTE_HOST_TYPING", None)
    assert patching._discord_typing_muted() is True  # default on (no config in test env)
    for val in ("false", "0", "no", "off", "FALSE"):
        os.environ["HERMES_DISCORD_MUTE_HOST_TYPING"] = val
        assert patching._discord_typing_muted() is False, f"{val} should disable"
    os.environ["HERMES_DISCORD_MUTE_HOST_TYPING"] = "true"
    assert patching._discord_typing_muted() is True
    del os.environ["HERMES_DISCORD_MUTE_HOST_TYPING"]


if __name__ == "__main__":
    test_discord_typing_muted_default_on_and_disableable()
    print("OK: default-on, false/0/no/off disable, true enables")
