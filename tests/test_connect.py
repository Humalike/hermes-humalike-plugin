"""Checks for the /connect device-authorization command.

connect.py imports httpx (not installed in this test env) and its siblings via
relative imports, so we stub httpx and load everything under a fake parent
package (the social_learning test's loader). Run directly:
python3 tests/test_connect.py
"""

import asyncio
import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _load():
    sys.modules.setdefault("httpx", types.ModuleType("httpx"))

    pkg = types.ModuleType("_humalike_test_pkg")
    pkg.__path__ = [str(_ROOT)]
    sys.modules["_humalike_test_pkg"] = pkg

    def _mod(name, path, package=None):
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        if package:
            mod.__package__ = package
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    _mod("_humalike_test_pkg._config", _ROOT / "_config.py")

    tt = types.ModuleType("_humalike_test_pkg.turn_taking")
    tt.__path__ = [str(_ROOT / "turn_taking")]
    sys.modules["_humalike_test_pkg.turn_taking"] = tt
    tt.state = _mod("_humalike_test_pkg.turn_taking.state",
                    _ROOT / "turn_taking" / "state.py", "_humalike_test_pkg.turn_taking")
    tt.notify = _mod("_humalike_test_pkg.turn_taking.notify",
                     _ROOT / "turn_taking" / "notify.py", "_humalike_test_pkg.turn_taking")

    return _mod("_humalike_test_pkg.connect", _ROOT / "connect.py", "_humalike_test_pkg")


connect = _load()


def _clean_env():
    for k in ("HUMALIKE_API_KEY", "HUMALIKE_API_URL", "HUMALIKE_CLI_GATEWAY_KEY"):
        os.environ.pop(k, None)


# ── _write_env: the .env upsert ───────────────────────────────────────────────
def test_write_env_creates_file_with_key():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "sub" / ".env"
        connect._write_env(p, "ak_new")
        assert p.read_text() == "HUMALIKE_API_KEY=ak_new\n"
        assert (p.stat().st_mode & 0o777) == 0o600


def test_write_env_replaces_key_and_preserves_other_lines():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / ".env"
        p.write_text("HUMALIKE_API_URL=https://api.humalike.com\n"
                     "HUMALIKE_API_KEY=ak_old\n"
                     "TELEGRAM_BOT_TOKEN=t\n")
        connect._write_env(p, "ak_new")
        lines = p.read_text().splitlines()
        assert "HUMALIKE_API_URL=https://api.humalike.com" in lines
        assert "TELEGRAM_BOT_TOKEN=t" in lines
        assert lines.count("HUMALIKE_API_KEY=ak_new") == 1
        assert not any("ak_old" in ln for ln in lines)


# ── command(): the guard rails (no HTTP reached) ──────────────────────────────
def test_command_already_connected():
    _clean_env()
    os.environ["HUMALIKE_API_KEY"] = "ak_x"
    try:
        reply = asyncio.run(connect.command(""))
    finally:
        _clean_env()
    assert "Already connected" in reply, reply


def test_command_without_gateway_key_points_to_manual_setup():
    _clean_env()
    reply = asyncio.run(connect.command(""))
    assert "HUMALIKE_API_KEY" in reply, reply  # manual fallback instructions


def test_command_single_flight():
    _clean_env()
    os.environ["HUMALIKE_CLI_GATEWAY_KEY"] = "gk"
    connect._PENDING.set()
    try:
        reply = asyncio.run(connect.command(""))
    finally:
        connect._PENDING.clear()
        _clean_env()
    assert "already waiting" in reply, reply


def test_command_failure_clears_pending():
    """The stubbed httpx has no AsyncClient, so the create call fails exactly
    like a network error — the guard flag must not stay stranded."""
    _clean_env()
    os.environ["HUMALIKE_CLI_GATEWAY_KEY"] = "gk"
    try:
        reply = asyncio.run(connect.command(""))
    finally:
        _clean_env()
    assert "Couldn't reach Humalike" in reply, reply
    assert not connect._PENDING.is_set()


def test_write_env_tightens_existing_file_mode():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / ".env"
        p.write_text("X=1\n")
        p.chmod(0o644)
        connect._write_env(p, "ak_new")
        assert (p.stat().st_mode & 0o777) == 0o600


# ── Outcome messages ──────────────────────────────────────────────────────────
def test_result_messages_offer_retry():
    assert "/connect" in connect._result_message("denied")
    assert "/connect" in connect._result_message("expired")


def test_done_message_names_account_and_depends_on_url():
    _clean_env()
    os.environ["HUMALIKE_API_URL"] = "https://api.humalike.com"
    try:
        m = connect._done_message({"account": {"email": "maks@example.com"}})
        assert "maks@example.com" in m and "active" in m
    finally:
        _clean_env()
    m = connect._done_message({})  # no URL configured, no email returned
    assert "your account" in m and "HUMALIKE_API_URL" in m


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all passed")
