"""Checks for the /connect device-authorization command and the login module.

connect.py imports httpx (not installed in this test env) and its siblings via
relative imports, so we stub httpx and load everything under a fake parent
package (the social_learning test's loader). login.py is stdlib-only. Run
directly:  python3 tests/test_connect.py
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
    lg = _mod("_humalike_test_pkg.login", _ROOT / "login.py", "_humalike_test_pkg")

    tt = types.ModuleType("_humalike_test_pkg.turn_taking")
    tt.__path__ = [str(_ROOT / "turn_taking")]
    sys.modules["_humalike_test_pkg.turn_taking"] = tt
    tt.state = _mod("_humalike_test_pkg.turn_taking.state",
                    _ROOT / "turn_taking" / "state.py", "_humalike_test_pkg.turn_taking")
    tt.notify = _mod("_humalike_test_pkg.turn_taking.notify",
                     _ROOT / "turn_taking" / "notify.py", "_humalike_test_pkg.turn_taking")

    cn = _mod("_humalike_test_pkg.connect", _ROOT / "connect.py", "_humalike_test_pkg")
    return cn, lg


connect, login = _load()

# Point every ~/.hermes read/write at a scratch dir — gateway_key()/cfg() read
# the real .env otherwise, which would make these tests machine-dependent.
_TMP = Path(tempfile.mkdtemp())
login.HERMES_ENV = _TMP / ".env"


def _clean_env():
    for k in ("HUMALIKE_API_KEY", "HUMALIKE_API_URL", "HUMALIKE_CLI_GATEWAY_KEY"):
        os.environ.pop(k, None)
    login._key_ok = None  # reset the per-process key-validation cache between tests


# ── login.write_env_key: the .env upsert ──────────────────────────────────────
def test_write_env_creates_file_with_key():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "sub" / ".env"
        login.write_env_key(p, "ak_new")
        assert p.read_text() == "HUMALIKE_API_KEY=ak_new\n"
        assert (p.stat().st_mode & 0o777) == 0o600


def test_write_env_replaces_key_and_preserves_other_lines():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / ".env"
        p.write_text("HUMALIKE_API_URL=https://api.humalike.com\n"
                     "HUMALIKE_API_KEY=ak_old\n"
                     "TELEGRAM_BOT_TOKEN=t\n")
        login.write_env_key(p, "ak_new")
        lines = p.read_text().splitlines()
        assert "HUMALIKE_API_URL=https://api.humalike.com" in lines
        assert "TELEGRAM_BOT_TOKEN=t" in lines
        assert lines.count("HUMALIKE_API_KEY=ak_new") == 1
        assert not any("ak_old" in ln for ln in lines)


def test_write_env_tightens_existing_file_mode():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / ".env"
        p.write_text("X=1\n")
        p.chmod(0o644)
        login.write_env_key(p, "ak_new")
        assert (p.stat().st_mode & 0o777) == 0o600


# ── login config readers ──────────────────────────────────────────────────────
def test_read_env_file_parses_and_skips_comments():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / ".env"
        p.write_text("# comment\nA=1\n\nB = spaced \nnot-a-pair\n")
        got = login.read_env_file(p)
        assert got == {"A": "1", "B": "spaced"}, got
    assert login.read_env_file(Path(d) / "missing") == {}


def test_cfg_env_wins_over_file():
    _clean_env()
    login.HERMES_ENV.write_text("HUMALIKE_CLI_GATEWAY_KEY=from_file\n")
    try:
        assert login.gateway_key() == "from_file"
        os.environ["HUMALIKE_CLI_GATEWAY_KEY"] = "from_env"
        assert login.gateway_key() == "from_env"
    finally:
        login.HERMES_ENV.unlink()
        _clean_env()


def test_gateway_key_has_baked_default():
    """Zero-config: a fresh install must carry the public client identifier."""
    _clean_env()
    assert login.gateway_key().startswith("hcg_")


# ── login.run: terminal short-circuits (no HTTP reached) ─────────────────────
def test_run_already_connected_is_success():
    _clean_env()
    os.environ["HUMALIKE_API_KEY"] = "ak_x"
    try:
        assert login.run() == 0
    finally:
        _clean_env()


def test_run_without_gateway_key_fails_with_manual_hint():
    _clean_env()
    orig, login.GATEWAY_KEY_DEFAULT = login.GATEWAY_KEY_DEFAULT, ""
    try:
        assert login.run() == 1
    finally:
        login.GATEWAY_KEY_DEFAULT = orig


# ── login.has_working_key: the source of truth ───────────────────────────────
def test_has_working_key_no_key_is_false():
    _clean_env()
    assert login.has_working_key() is False


def test_has_working_key_present_key_without_probe_is_true():
    """WHOAMI_PATH unset → a present key counts as working, no network call."""
    _clean_env()
    os.environ["HUMALIKE_API_KEY"] = "ak_x"
    orig, login.WHOAMI_PATH = login.WHOAMI_PATH, None
    try:
        assert login.has_working_key() is True
    finally:
        login.WHOAMI_PATH = orig
        _clean_env()


def test_probe_rejects_only_on_401_403():
    """401/403 → key dead (False); every other error → assume live (True), so an
    API outage never forces a needless re-login."""
    import urllib.error

    def _raise(code):
        def _p(*a, **k):
            raise urllib.error.HTTPError("u", code, "x", {}, None)
        return _p

    orig = login.urllib.request.urlopen
    orig_path, login.WHOAMI_PATH = login.WHOAMI_PATH, "/v1/keys/actions/whoami"
    try:
        login.urllib.request.urlopen = _raise(401)
        assert login._probe("ak_x") is False
        login.urllib.request.urlopen = _raise(403)
        assert login._probe("ak_x") is False
        login.urllib.request.urlopen = _raise(500)
        assert login._probe("ak_x") is True          # server blip → assume live
        def _boom(*a, **k):
            raise OSError("network down")
        login.urllib.request.urlopen = _boom
        assert login._probe("ak_x") is True          # unreachable → assume live
    finally:
        login.urllib.request.urlopen = orig
        login.WHOAMI_PATH = orig_path


# ── login.maybe_first_boot_login: fires whenever there is no working key ──────
def _capture_threads():
    started = []

    class _T:
        def __init__(self, **kw):
            started.append(kw)

        def start(self):
            pass

    return started, types.SimpleNamespace(Thread=_T)


def test_first_boot_login_fires_when_no_working_key():
    """No marker: with a gateway key and no API key, every call pops the login
    (self-healing — an unseen/interrupted login re-offers next boot)."""
    _clean_env()
    os.environ["HUMALIKE_CLI_GATEWAY_KEY"] = "gk"
    started, fake = _capture_threads()
    orig_threading, login.threading = login.threading, fake
    try:
        login.maybe_first_boot_login()
        login._key_ok = None                 # simulate a fresh boot, still keyless
        login.maybe_first_boot_login()
        assert len(started) == 2             # fires again — no once-marker
    finally:
        login.threading = orig_threading
        _clean_env()


def test_first_boot_login_skipped_when_connected_or_unconfigured():
    _clean_env()
    started, fake = _capture_threads()
    orig_threading, login.threading = login.threading, fake
    orig, login.GATEWAY_KEY_DEFAULT = login.GATEWAY_KEY_DEFAULT, ""
    try:
        login.maybe_first_boot_login()       # no gateway key → no-op
        assert started == []
    finally:
        login.GATEWAY_KEY_DEFAULT = orig
    _clean_env()
    os.environ.update(HUMALIKE_CLI_GATEWAY_KEY="gk", HUMALIKE_API_KEY="ak_x")
    try:
        login.maybe_first_boot_login()       # working key present → no-op
        assert started == []
    finally:
        login.threading = orig_threading
        _clean_env()


# ── connect.command(): the guard rails (no HTTP reached) ─────────────────────
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
    orig, login.GATEWAY_KEY_DEFAULT = login.GATEWAY_KEY_DEFAULT, ""
    try:
        reply = asyncio.run(connect.command(""))
    finally:
        login.GATEWAY_KEY_DEFAULT = orig
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


def test_command_reshows_pending_first_boot_link():
    """A TUI banner can hide the first-boot popup's print — /connect must
    re-show the pending link, not mint a competing session."""
    _clean_env()
    login.PENDING_URI = "https://humalike.com/cli/auth?code=hcu_x"
    try:
        reply = asyncio.run(connect.command(""))
    finally:
        login.PENDING_URI = None
        _clean_env()
    assert "hcu_x" in reply, reply


def test_poll_session_expired_ttl_is_instant():
    got = login.poll_session({"expires_in": 0, "device_code": "hcd_x"}, "gk")
    assert got == {"status": "expired"}, got


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


# ── Outcome messages ──────────────────────────────────────────────────────────
def test_result_messages_offer_retry():
    assert "/connect" in connect._result_message("denied")
    assert "/connect" in connect._result_message("expired")


def test_done_message_names_account_and_depends_on_url():
    _clean_env()
    try:
        # URL defaults now → active without any env setup
        m = connect._done_message({"account": {"email": "maks@example.com"}})
        assert "maks@example.com" in m and "active" in m
        os.environ["HUMALIKE_API_URL"] = ""  # explicit opt-out
        m = connect._done_message({})
        assert "your account" in m and "HUMALIKE_API_URL" in m
    finally:
        _clean_env()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all passed")
