"""``turn_taking.pacing`` in config.yaml rides on every respond as ``pacing``.

The service's PacingOverrides (reading_delay_ms, typing_wpm, max_typing_ms)
paces this agent's replies only; unset config falls back to the plugin's own
default (_DEFAULT_PACING). Run directly:  python3 tests/test_service_pacing.py
"""

import asyncio
import importlib.util
import sys
import tempfile
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _load_service():
    sys.modules.setdefault("httpx", types.ModuleType("httpx"))
    httpx = sys.modules["httpx"]
    if not hasattr(httpx, "HTTPError"):
        httpx.HTTPStatusError = type("HTTPStatusError", (Exception,), {})
        httpx.HTTPError = type("HTTPError", (Exception,), {})

    pkg = types.ModuleType("_hum_pacing_pkg")
    pkg.__path__ = [str(_ROOT)]
    sys.modules["_hum_pacing_pkg"] = pkg
    tt = types.ModuleType("_hum_pacing_pkg.turn_taking")
    tt.__path__ = [str(_ROOT / "turn_taking")]
    sys.modules["_hum_pacing_pkg.turn_taking"] = tt

    def _mod(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        m = importlib.util.module_from_spec(spec)
        sys.modules[name] = m
        spec.loader.exec_module(m)
        return m

    _mod("_hum_pacing_pkg._config", _ROOT / "_config.py")
    _mod("_hum_pacing_pkg.turn_taking.state", _ROOT / "turn_taking" / "state.py")
    _mod("_hum_pacing_pkg.turn_taking.notify", _ROOT / "turn_taking" / "notify.py")
    return _mod("_hum_pacing_pkg.turn_taking.service", _ROOT / "turn_taking" / "service.py")


svc = _load_service()


def _respond_body(config_text):
    """Run respond() against a temp config.yaml; return the posted body."""
    tmp = Path(tempfile.mkdtemp()) / "config.yaml"
    tmp.write_text(config_text)
    captured = {}

    async def fake_post(path, body):
        captured[path] = body
        return {"scheduled": True}

    orig_cfg, orig_post = svc._HERMES_CONFIG, svc._post
    svc._HERMES_CONFIG, svc._post = tmp, fake_post
    try:
        asyncio.run(svc.respond("tid", "draft", 3))
    finally:
        svc._HERMES_CONFIG, svc._post = orig_cfg, orig_post
    return captured[svc.RESPOND_PATH]


def test_configured_pacing_rides_on_respond():
    body = _respond_body("turn_taking:\n  pacing:\n    typing_wpm: 40\n    reading_delay_ms: 0\n")
    assert body["pacing"] == {"typing_wpm": 40, "reading_delay_ms": 0}
    assert body["turn_epoch"] == 3  # rest of the body untouched


def test_no_pacing_config_falls_back_to_plugin_default():
    body = _respond_body("streaming: false\n")
    assert body["pacing"] == svc._DEFAULT_PACING == {"typing_wpm": 115}


def test_empty_or_garbage_pacing_falls_back_to_default():
    assert _respond_body("turn_taking:\n  pacing: {}\n")["pacing"] == svc._DEFAULT_PACING
    assert _respond_body("turn_taking:\n  pacing: fast\n")["pacing"] == svc._DEFAULT_PACING


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all ok")
