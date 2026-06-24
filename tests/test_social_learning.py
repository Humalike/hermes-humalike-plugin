"""Checks for the embedded social-learning voice-card module.

social_learning.py imports httpx (not installed in this test env), so we stub it
and load the module by file path. Run directly:  python3 tests/test_social_learning.py
"""

import importlib.util
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _load():
    sys.modules.setdefault("httpx", types.ModuleType("httpx"))
    spec = importlib.util.spec_from_file_location("sl", _ROOT / "social_learning.py")
    mod = importlib.util.module_from_spec(spec)
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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all ok")
