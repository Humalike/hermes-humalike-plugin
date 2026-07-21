"""Social-memory integration: open_thread enables it, and the context recalled
at decide is injected into the reply draft (pre_llm_call) and folded into the
respond system_prompt.

turn_taking imports httpx and its relative siblings, so we stub httpx and load
the modules under a fake package (like test_service_pacing), stubbing core +
delivery so hooks loads without the full chain. Run directly:
  python3 tests/test_social_memory.py
"""

import asyncio
import importlib.util
import os
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PKG = "_hum_sm_pkg"


def _load():
    sys.modules.setdefault("httpx", types.ModuleType("httpx"))
    httpx = sys.modules["httpx"]
    if not hasattr(httpx, "HTTPError"):
        httpx.HTTPStatusError = type("HTTPStatusError", (Exception,), {})
        httpx.HTTPError = type("HTTPError", (Exception,), {})

    pkg = types.ModuleType(_PKG)
    pkg.__path__ = [str(_ROOT)]
    sys.modules[_PKG] = pkg
    tt = types.ModuleType(_PKG + ".turn_taking")
    tt.__path__ = [str(_ROOT / "turn_taking")]
    sys.modules[_PKG + ".turn_taking"] = tt

    def _mod(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        m = importlib.util.module_from_spec(spec)
        sys.modules[name] = m
        spec.loader.exec_module(m)
        return m

    _mod(_PKG + "._config", _ROOT / "_config.py")
    state = _mod(_PKG + ".turn_taking.state", _ROOT / "turn_taking" / "state.py")
    _mod(_PKG + ".turn_taking.notify", _ROOT / "turn_taking" / "notify.py")
    service = _mod(_PKG + ".turn_taking.service", _ROOT / "turn_taking" / "service.py")

    # Stub core + delivery so hooks imports resolve without loading the chain.
    core = types.ModuleType(_PKG + ".turn_taking.core")
    core._build_system_prompt_for_turn_taking = lambda adapter, sid: "PERSONA"

    async def _respond(*a, **k):
        return True

    core._respond = _respond
    sys.modules[_PKG + ".turn_taking.core"] = core
    delivery = types.ModuleType(_PKG + ".turn_taking.delivery")
    delivery._chat_for_session = lambda sid: None
    sys.modules[_PKG + ".turn_taking.delivery"] = delivery

    hooks = _mod(_PKG + ".turn_taking.hooks", _ROOT / "turn_taking" / "hooks.py")
    return state, service, hooks


STATE, SVC, HOOKS = _load()


def _open_body(**env):
    """Call open_thread with env overrides; return the posted body."""
    captured = {}

    async def fake_post(path, body):
        captured["body"] = body
        return {"thread": {"id": "t1"}}

    orig_post = SVC._post
    SVC._post = fake_post
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        asyncio.run(SVC.open_thread("t1"))
    finally:
        SVC._post = orig_post
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    return captured["body"]


def test_open_thread_enables_social_memory_with_bank_id():
    body = _open_body(HUMALIKE_MEMORY_BANK_ID="agent-42")
    assert body["thread_id"] == "t1"
    assert body["integrations"]["social_memory"]["memory_bank_id"] == "agent-42"


def test_bank_id_override_is_capped():
    body = _open_body(HUMALIKE_MEMORY_BANK_ID="x" * 300)
    assert len(body["integrations"]["social_memory"]["memory_bank_id"]) == 255


def test_pre_llm_call_injects_memory_only_when_present():
    STATE.MEMORY_BY_SESSION.clear()
    assert HOOKS.on_pre_llm_call(session_id="s1") is None  # no memory -> no inject
    STATE.MEMORY_BY_SESSION["s1"] = "alice has churned before; wants terse replies"
    out = HOOKS.on_pre_llm_call(session_id="s1")
    assert out and "alice has churned before" in out["context"]
    STATE.MEMORY_BY_SESSION["s2"] = ""  # empty -> no inject
    assert HOOKS.on_pre_llm_call(session_id="s2") is None
    assert HOOKS.on_pre_llm_call(session_id="") is None  # no session -> no inject


def test_with_memory_folds_into_respond_system_prompt():
    STATE.MEMORY_BY_SESSION.clear()
    assert HOOKS._with_memory("PERSONA", "s1") == "PERSONA"  # no memory -> unchanged
    STATE.MEMORY_BY_SESSION["s1"] = "bob prefers technical detail"
    out = HOOKS._with_memory("PERSONA", "s1")
    assert out.startswith("PERSONA") and "bob prefers technical detail" in out
    # No base prompt -> just the memory block.
    assert HOOKS._with_memory(None, "s1").endswith("bob prefers technical detail")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all ok")
