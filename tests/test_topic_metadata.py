"""Round-trip of the opaque per-turn delivery metadata (forum-topic placement).

Checks the three ends of the metadata round-trip without a live service:
  - decide side extracts the topic from event.source.thread_id (_delivery_meta),
  - respond puts it in the wire body (service.respond),
  - delivery hands it to send so the bubble lands in the topic (_forward).

Run directly:  python3 tests/test_topic_metadata.py
"""

import asyncio
import importlib
import sys
import types
from pathlib import Path

# The repo dir name has hyphens (not a valid module name), and turn_taking/core
# does `from .. import social_learning`, so it must load as a SUBpackage. Mount a
# synthetic parent package rooted at the repo (same trick as test_to_messages.py).
_ROOT = Path(__file__).resolve().parent.parent
sys.modules.setdefault("httpx", types.ModuleType("httpx"))
_pkg = types.ModuleType("tt_pkg")
_pkg.__path__ = [str(_ROOT)]
sys.modules["tt_pkg"] = _pkg
core = importlib.import_module("tt_pkg.turn_taking.core")
state = importlib.import_module("tt_pkg.turn_taking.state")
service = importlib.import_module("tt_pkg.turn_taking.service")
delivery = importlib.import_module("tt_pkg.turn_taking.delivery")


def _event(thread_id):
    return types.SimpleNamespace(source=types.SimpleNamespace(thread_id=thread_id))


def test_delivery_meta_reads_topic():
    assert core._delivery_meta(_event("5")) == {"thread_id": "5"}
    assert core._delivery_meta(_event(None)) == {}  # no topic → no hint
    assert core._delivery_meta(_event(7)) == {"thread_id": "7"}  # coerced to str


def test_respond_puts_metadata_in_body():
    captured = {}

    async def fake_post(path, body):
        captured["body"] = body
        return {"scheduled": []}

    service._post = fake_post
    asyncio.run(service.respond("tid", "hej", 7, None, {"thread_id": "5"}))
    assert captured["body"]["metadata"] == {"thread_id": "5"}
    captured.clear()
    asyncio.run(service.respond("tid", "hej", 7, None, None))
    assert "metadata" not in captured["body"]  # omitted when none


def test_forward_passes_topic_to_send():
    sent = {}

    class FakeAdapter:
        pass

    async def fake_orig(adapter, chat_id, content, reply_to=None, metadata=None):
        sent.update(chat_id=chat_id, content=content, metadata=metadata)

    adapter = FakeAdapter()
    state.ORIG_SEND[FakeAdapter] = fake_orig
    state.ROUTES["T1"] = (adapter, "-100grp")
    asyncio.run(delivery._forward("T1", "bubble", {"thread_id": "5"}))
    assert sent == {"chat_id": "-100grp", "content": "bubble", "metadata": {"thread_id": "5"}}


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all passed")
