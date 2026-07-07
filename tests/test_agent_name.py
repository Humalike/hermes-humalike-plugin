"""`agent_name` on the respond wire call + the `_agent_name()` resolver.

The service uses agent_name to label the bot's own lines in the theory-of-mind
transcript (mismatched identity made small models reply in the third person /
to themselves). Loads service.py standalone with stubbed httpx, same pattern as
test_service_keyless. Run directly:  python3 tests/test_agent_name.py
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
    sys.modules["httpx"] = httpx

    pkg = types.ModuleType("_hum_an_pkg")
    pkg.__path__ = [str(_ROOT)]
    sys.modules["_hum_an_pkg"] = pkg
    tt = types.ModuleType("_hum_an_pkg.turn_taking")
    tt.__path__ = [str(_ROOT / "turn_taking")]
    sys.modules["_hum_an_pkg.turn_taking"] = tt

    def _mod(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        m = importlib.util.module_from_spec(spec)
        sys.modules[name] = m
        spec.loader.exec_module(m)
        return m

    _mod("_hum_an_pkg._config", _ROOT / "_config.py")
    _mod("_hum_an_pkg.turn_taking.state", _ROOT / "turn_taking" / "state.py")
    _mod("_hum_an_pkg.turn_taking.notify", _ROOT / "turn_taking" / "notify.py")
    return _mod("_hum_an_pkg.turn_taking.service", _ROOT / "turn_taking" / "service.py")


svc = _load_service()


def _respond_body(**kwargs):
    """Call svc.respond with _post recorded; return the wire body."""
    sent = {}

    async def _post(path, body):
        sent["path"], sent["body"] = path, body
        return {"scheduled": []}

    with patch.object(svc, "_post", _post):
        asyncio.run(svc.respond("tid", "draft", 3, **kwargs))
    return sent["body"]


def test_respond_sends_agent_name():
    body = _respond_body(agent_name="Hermes")
    assert body["agent_name"] == "Hermes"


def test_respond_omits_agent_name_when_unset():
    assert "agent_name" not in _respond_body()
    assert "agent_name" not in _respond_body(agent_name="")


def test_respond_caps_agent_name_to_contract_limit():
    assert len(_respond_body(agent_name="x" * 300)["agent_name"]) == 255


def test_agent_name_env_override_and_config_fallback(tmp_path=None):
    # core._agent_name reads HERMES_AGENT_NAME first, then agent.name in
    # ~/.hermes/config.yaml. Exercise both without loading all of core.py's
    # imports: point its _HERMES_CONFIG at a temp file.
    import tempfile

    core = _load_core()
    with patch.dict(os.environ, {"HERMES_AGENT_NAME": "EnvBot"}, clear=False):
        assert core._agent_name() == "EnvBot"
    os.environ.pop("HERMES_AGENT_NAME", None)
    with tempfile.TemporaryDirectory() as d:
        cfg = Path(d) / "config.yaml"
        cfg.write_text("agent:\n  name: Hermes\n")
        with patch.object(core, "_HERMES_CONFIG", cfg):
            assert core._agent_name() == "Hermes"
        cfg.write_text("agent: {}\n")
        with patch.object(core, "_HERMES_CONFIG", cfg):
            assert core._agent_name() is None


def _load_core():
    """Load core.py with its sibling imports stubbed (delivery, social_learning)."""
    def _stub(name, **attrs):
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m
        return m

    _stub("_hum_an_pkg.social_learning", get_card=lambda sid: None)
    _stub("_hum_an_pkg.turn_taking.delivery", _ensure_thread=None, _chat_for_session=None)
    if "yaml" not in sys.modules:
        # PyYAML ships with the gateway but not necessarily this test env; the
        # tests only feed `agent:\n  name: X` / `agent: {}`, so a two-line
        # parser is enough.
        def _safe_load(text):
            lines = [l for l in text.splitlines() if l.strip()]
            if lines == ["agent: {}"]:
                return {"agent": {}}
            return {"agent": {"name": lines[1].split(":", 1)[1].strip()}}

        _stub("yaml", safe_load=_safe_load)
    spec = importlib.util.spec_from_file_location(
        "_hum_an_pkg.turn_taking.core", _ROOT / "turn_taking" / "core.py"
    )
    m = importlib.util.module_from_spec(spec)
    sys.modules["_hum_an_pkg.turn_taking.core"] = m
    spec.loader.exec_module(m)
    return m


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all ok")
