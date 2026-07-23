"""Checks for `_to_messages` — the media-aware decide-payload builder.

`_to_messages` lives in turn_taking/service.py, which uses relative imports, so
we stub httpx and load the package by file path under a throwaway package name.
Run directly:  python3 tests/test_to_messages.py
"""

import importlib.util
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _load_plugin():
    sys.modules.setdefault("httpx", types.ModuleType("httpx"))
    httpx = sys.modules["httpx"]
    if not hasattr(httpx, "HTTPError"):
        httpx.HTTPStatusError = type("HTTPStatusError", (Exception,), {})
        httpx.HTTPError = type("HTTPError", (Exception,), {})

    pkg = types.ModuleType("tt_pkg")
    pkg.__path__ = [str(_ROOT)]
    sys.modules["tt_pkg"] = pkg
    tt = types.ModuleType("tt_pkg.turn_taking")
    tt.__path__ = [str(_ROOT / "turn_taking")]
    sys.modules["tt_pkg.turn_taking"] = tt

    def _mod(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        m = importlib.util.module_from_spec(spec)
        sys.modules[name] = m
        spec.loader.exec_module(m)
        return m

    _mod("tt_pkg._config", _ROOT / "_config.py")
    _mod("tt_pkg.turn_taking.state", _ROOT / "turn_taking" / "state.py")
    _mod("tt_pkg.turn_taking.notify", _ROOT / "turn_taking" / "notify.py")
    return _mod("tt_pkg.turn_taking.service", _ROOT / "turn_taking" / "service.py")


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


def test_discord_host_placeholder_treated_as_captionless_media():
    # Discord fills captionless media text with a host placeholder sentence;
    # it must not leak into the service transcript as content.
    out = tt._to_messages([_event(
        text="(The user sent a message with no text content)",
        mtype="photo", media=["http://x/img.png"],
    )])
    assert out == [{"sender": "alice", "content": "[image]", "has_media": True}]


def test_host_placeholder_without_media_is_kept_as_text():
    # A user literally typing the host's placeholder sentence (no media attached)
    # must keep their text — the blanking is gated on has_media.
    out = tt._to_messages([_event(text="(The user sent a message with no text content)")])
    assert out == [{"sender": "alice", "content": "(The user sent a message with no text content)"}]


def test_text_message_has_no_media_key():
    assert "has_media" not in tt._to_messages([_event(text="hi")])[0]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all ok")
