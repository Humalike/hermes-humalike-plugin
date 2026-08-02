"""Checks the API-alert status classification. Run: python3 tests/test_notify.py."""

import importlib.util
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _load_notify():
    pkg = types.ModuleType("_notify_test_pkg")
    pkg.__path__ = [str(_ROOT)]
    sys.modules["_notify_test_pkg"] = pkg
    tt = types.ModuleType("_notify_test_pkg.turn_taking")
    tt.__path__ = [str(_ROOT / "turn_taking")]
    sys.modules["_notify_test_pkg.turn_taking"] = tt
    for name in ("state", "notify"):
        spec = importlib.util.spec_from_file_location(
            f"_notify_test_pkg.turn_taking.{name}", _ROOT / "turn_taking" / f"{name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return sys.modules["_notify_test_pkg.turn_taking.notify"]


def test_auth_alert_only_for_401():
    notify = _load_notify()
    assert notify._kind(401) == "auth"
    assert notify._why(401) == "API key rejected — check HUMALIKE_API_KEY"
    assert notify._kind(403) == "server"
    assert notify._why(403) == (
        "couldn't process a request (HTTP 403) — your API key may still be valid; "
        "try again later or contact Humalike support"
    )


if __name__ == "__main__":
    test_auth_alert_only_for_401()
    print("all tests passed")
