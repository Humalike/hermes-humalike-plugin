"""Checks for `_to_messages` — the media-aware decide-payload builder.

The plugin's __init__.py needs the Hermes loader, so we stub the modules it
imports (httpx, soul deps) and load it by file path under a throwaway package
name. Run directly:  python3 tests/test_to_messages.py
"""

import importlib.util
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _load_plugin():
    sys.modules.setdefault("httpx", types.ModuleType("httpx"))
    pkg = types.ModuleType("tt_pkg")
    pkg.__path__ = [str(_ROOT)]
    sys.modules["tt_pkg"] = pkg
    spec = importlib.util.spec_from_file_location("tt_pkg.__init__", _ROOT / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["tt_pkg.__init__"] = mod
    spec.loader.exec_module(mod)
    return mod


def _event(text="", mtype="text", media=None, sender="alice"):
    return types.SimpleNamespace(
        text=text,
        message_type=types.SimpleNamespace(value=mtype),
        media_urls=media or [],
        source=types.SimpleNamespace(user_name=sender),
    )


tt = _load_plugin()


def test_plain_text():
    assert tt._to_messages([_event(text="hi")]) == [{"sender": "alice", "content": "hi"}]


def test_empty_text_skipped():
    assert tt._to_messages([_event(text="   ")]) == []


def test_captionless_media_gets_placeholder_and_flag():
    # Image with no caption: placeholder content + has_media, NOT skipped.
    out = tt._to_messages([_event(mtype="photo", media=["/tmp/x.jpg"])])
    assert out == [{"sender": "alice", "content": "[image]", "has_media": True}]


def test_each_media_type_placeholder():
    cases = {
        "photo": "[image]",
        "video": "[video]",
        "voice": "[voice message]",
        "document": "[document]",
        "sticker": "[sticker]",
    }
    for mtype, placeholder in cases.items():
        out = tt._to_messages([_event(mtype=mtype, media=["/tmp/f"])])
        assert out[0]["content"] == placeholder, (mtype, out)
        assert out[0]["has_media"] is True


def test_captioned_media_keeps_caption_and_flag():
    out = tt._to_messages([_event(text="look!", mtype="photo", media=["/tmp/x.jpg"])])
    assert out == [{"sender": "alice", "content": "look!", "has_media": True}]


def test_media_detected_by_urls_even_if_type_text():
    # Defensive: media attached but type slipped through as text → still flagged.
    out = tt._to_messages([_event(text="", mtype="text", media=["/tmp/x.jpg"])])
    assert out[0]["has_media"] is True
    assert out[0]["content"] == "[media]"


def test_text_message_has_no_media_key():
    assert "has_media" not in tt._to_messages([_event(text="hi")])[0]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all ok")
