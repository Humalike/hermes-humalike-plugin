"""Checks for the embedded social-learning voice-card module.

social_learning/ imports httpx (not installed in this test env) and its sibling
_config.py via a relative import, so we stub httpx and load both under a fake
parent package. Run directly:  python3 tests/test_social_learning.py
"""

import importlib.util
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _load():
    sys.modules.setdefault("httpx", types.ModuleType("httpx"))

    pkg = types.ModuleType("_humalike_test_pkg")
    pkg.__path__ = [str(_ROOT)]
    sys.modules["_humalike_test_pkg"] = pkg

    cfg_spec = importlib.util.spec_from_file_location("_humalike_test_pkg._config", _ROOT / "_config.py")
    cfg = importlib.util.module_from_spec(cfg_spec)
    sys.modules["_humalike_test_pkg._config"] = cfg
    cfg_spec.loader.exec_module(cfg)

    spec = importlib.util.spec_from_file_location(
        "_humalike_test_pkg.social_learning", _ROOT / "social_learning" / "__init__.py"
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "_humalike_test_pkg.social_learning"
    sys.modules["_humalike_test_pkg.social_learning"] = mod
    spec.loader.exec_module(mod)
    return mod


sl = _load()


def test_transcript_lifts_author_and_drops_control_markers():
    hist = [
        {"role": "user", "content": "[New message]\n[Marti] you did it again\n[Marti] he doesn't learn?"},
        {"role": "assistant", "content": "my bad"},  # non-user dropped
        {"role": "user", "content": "plain dm line"},
    ]
    t = sl._build_transcript(hist)
    # Wire field is ``speaker`` (social_norms Message schema).
    assert [m["speaker"] for m in t] == ["Marti", "Marti", "user"], t
    assert [m["text"] for m in t] == ["you did it again", "he doesn't learn?", "plain dm line"], t
    assert all("id" in m for m in t)


def test_inject_returns_none_without_card():
    sl._CACHE.pop("sX", None)
    assert sl.on_pre_llm_call(session_id="sX", conversation_history=[]) is None


def test_inject_returns_context_with_card():
    sl._CACHE["sX"] = "# VOICE CARD\nCasing: lowercase"
    try:
        assert sl.on_pre_llm_call(session_id="sX") == {"context": "# VOICE CARD\nCasing: lowercase"}
    finally:
        sl._CACHE.pop("sX", None)


def test_no_session_id_is_noop():
    assert sl.on_pre_llm_call(session_id="") is None


def test_cache_survives_reload_from_disk():
    """_save_cache() writes; a fresh _load_cache() on an empty _CACHE restores it —
    the actual restart-persistence behavior this test is for."""
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp())
    orig_cache_file = sl._cache_file
    sl._cache_file = lambda: tmp / "social-learning-cache.json"
    try:
        sl._CACHE["restart-test"] = "voice card text"
        sl._save_cache()
        assert (tmp / "social-learning-cache.json").exists()

        sl._CACHE.clear()
        sl._COUNTER.clear()
        sl._load_cache()
        assert sl._CACHE.get("restart-test") == "voice card text"
    finally:
        sl._cache_file = orig_cache_file
        sl._CACHE.pop("restart-test", None)


def test_warm_recent_sessions_skips_already_cached():
    """Fires _refresh_card for uncached recent sessions only, using each
    session's DB-restored history (not the live pre_llm_call kwarg)."""
    import tempfile
    import threading

    tmp = Path(tempfile.mkdtemp())
    (tmp / "state.db").write_text("x")

    fake_hc = types.ModuleType("hermes_constants")
    fake_hc.get_hermes_home = lambda: tmp
    sys.modules["hermes_constants"] = fake_hc

    class FakeDB:
        def __init__(self, db_path, read_only):
            pass

        def list_sessions_rich(self, limit, order_by_last_active):
            return [{"id": "warm-1"}, {"id": "warm-2"}]

        def get_messages_as_conversation(self, session_id):
            return [{"role": "user", "content": "hello there"}]

    fake_hs = types.ModuleType("hermes_state")
    fake_hs.SessionDB = FakeDB
    sys.modules["hermes_state"] = fake_hs

    class SyncThread:
        def __init__(self, target=None, args=(), daemon=None):
            self._target, self._args = target, args

        def start(self):
            self._target(*self._args)

    sl._CACHE.pop("warm-1", None)
    sl._CACHE["warm-2"] = "already cached — should be skipped"
    calls = []
    orig_get_url, orig_refresh, orig_thread = sl._get_service_url, sl._refresh_card, threading.Thread
    sl._get_service_url = lambda: "http://example.test"
    sl._refresh_card = lambda session_id, history: calls.append(session_id)
    threading.Thread = SyncThread
    try:
        sl.warm_recent_sessions()
    finally:
        threading.Thread = orig_thread
        sl._get_service_url = orig_get_url
        sl._refresh_card = orig_refresh
        sl._CACHE.pop("warm-2", None)
        sys.modules.pop("hermes_constants", None)
        sys.modules.pop("hermes_state", None)

    assert calls == ["warm-1"], calls


def test_spawn_refresh_dedups_in_flight():
    """While a refresh runs for a session, further spawns for it are skipped."""
    import threading

    calls = []
    orig_refresh, orig_thread = sl._refresh_card, threading.Thread
    sl._refresh_card = lambda session_id, history: calls.append(session_id)

    class SyncThread:
        def __init__(self, target=None, args=(), daemon=None):
            self._target, self._args = target, args

        def start(self):
            self._target(*self._args)

    threading.Thread = SyncThread
    try:
        sl._REFRESHING.add("dup-1")                      # simulate one in flight
        assert sl._spawn_refresh("dup-1", []) is False
        assert calls == []
        sl._REFRESHING.discard("dup-1")
        assert sl._spawn_refresh("dup-1", []) is True
        assert calls == ["dup-1"]
        assert "dup-1" not in sl._REFRESHING             # cleaned up after the run
    finally:
        threading.Thread = orig_thread
        sl._refresh_card = orig_refresh
        sl._REFRESHING.discard("dup-1")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all ok")
