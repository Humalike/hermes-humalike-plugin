"""The transport gate: keyless installs must not POST conversation content.

service._post returns None before touching httpx when HUMALIKE_API_KEY is
unset (the URL now defaults to the public API, so key presence is the real
on/off switch). httpx is stubbed WITHOUT AsyncClient, so any real send
attempt raises AttributeError. Run directly:  python3 tests/test_service_keyless.py
"""

import asyncio
import importlib.util
import os
import sys
import types
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parent.parent


def _load_service():
    httpx = types.ModuleType("httpx")

    class _Err(Exception):
        pass

    httpx.HTTPStatusError = _Err
    httpx.HTTPError = _Err
    # no AsyncClient on purpose: a real send attempt -> AttributeError
    sys.modules["httpx"] = httpx

    pkg = types.ModuleType("_hum_svc_pkg")
    pkg.__path__ = [str(_ROOT)]
    sys.modules["_hum_svc_pkg"] = pkg
    tt = types.ModuleType("_hum_svc_pkg.turn_taking")
    tt.__path__ = [str(_ROOT / "turn_taking")]
    sys.modules["_hum_svc_pkg.turn_taking"] = tt

    def _mod(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        m = importlib.util.module_from_spec(spec)
        sys.modules[name] = m
        spec.loader.exec_module(m)
        return m

    _mod("_hum_svc_pkg._config", _ROOT / "_config.py")
    _mod("_hum_svc_pkg.turn_taking.state", _ROOT / "turn_taking" / "state.py")
    _mod("_hum_svc_pkg.turn_taking.notify", _ROOT / "turn_taking" / "notify.py")
    return _mod("_hum_svc_pkg.turn_taking.service", _ROOT / "turn_taking" / "service.py")


svc = _load_service()


def test_keyless_post_is_a_noop():
    with patch.dict(os.environ, {"HUMALIKE_API_URL": "http://example.test"}, clear=False):
        os.environ.pop("HUMALIKE_API_KEY", None)
        assert asyncio.run(svc._post("/x", {"content": "private transcript"})) is None


def test_with_key_post_actually_attempts_http():
    with patch.dict(os.environ, {"HUMALIKE_API_URL": "http://example.test",
                                 "HUMALIKE_API_KEY": "k"}, clear=False):
        try:
            asyncio.run(svc._post("/x", {}))
        except AttributeError:
            return  # reached the stubbed-out httpx client — the gate isn't over-blocking
        raise AssertionError("expected an HTTP attempt when a key is set")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all ok")
