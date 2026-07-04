"""Checks for native_memory — stripping native memory's *style* capture.

native_memory.py defers its Hermes imports (``tools.*``, ``agent.*``) into the
two strip functions, so we load the module plainly and stub those host modules
in ``sys.modules`` before calling. Stdlib-only, no network. Run directly:
    python3 tests/test_native_memory.py
"""

import importlib.util
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# The style-flavored example native_memory removes — must match the literal in
# _strip_guidance_style() so the seeded guidance actually contains it.
_STYLE_EXAMPLE = "'User prefers concise responses' ✓ — 'Always respond concisely' ✗. "


def _load():
    spec = importlib.util.spec_from_file_location("native_memory", _ROOT / "native_memory.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _install_tools(schema_desc):
    """Fake ``tools.memory_tool`` + ``tools.registry`` with a live memory schema
    dict. Returns the schema dict so the caller can read it back after a patch."""
    schema = {"description": schema_desc}
    entry = types.SimpleNamespace(schema=schema)
    registry = types.SimpleNamespace(
        get_entry=lambda name: entry if name == "memory" else None
    )
    tools = types.ModuleType("tools")
    tools_registry = types.ModuleType("tools.registry")
    tools_registry.registry = registry
    tools.registry = tools_registry
    sys.modules["tools"] = tools
    sys.modules["tools.registry"] = tools_registry
    sys.modules["tools.memory_tool"] = types.ModuleType("tools.memory_tool")
    return schema


def _install_agent(guidance):
    """Fake ``agent.prompt_builder`` + ``agent.system_prompt`` carrying
    MEMORY_GUIDANCE. Returns (pb, sp) so the caller can read them back."""
    pb = types.ModuleType("agent.prompt_builder")
    sp = types.ModuleType("agent.system_prompt")
    pb.MEMORY_GUIDANCE = guidance
    sp.MEMORY_GUIDANCE = guidance
    agent = types.ModuleType("agent")
    agent.prompt_builder = pb
    agent.system_prompt = sp
    sys.modules["agent"] = agent
    sys.modules["agent.prompt_builder"] = pb
    sys.modules["agent.system_prompt"] = sp
    return pb, sp


def _clear_host():
    for name in ("tools", "tools.registry", "tools.memory_tool",
                 "agent", "agent.prompt_builder", "agent.system_prompt"):
        sys.modules.pop(name, None)


def test_schema_strip_and_idempotent():
    nm = _load()
    schema = _install_tools(
        "Save facts about the user: name, role, preferences, communication style, environment."
    )
    assert nm._strip_schema_style() is True
    desc = schema["description"]
    assert "communication style" not in desc, "style clause should be gone"
    assert nm._PROHIBITION in desc, "prohibition should be appended"
    # Idempotent: a second call changes nothing and never double-appends.
    assert nm._strip_schema_style() is False
    assert schema["description"].count(nm._PROHIBITION) == 1
    _clear_host()


def test_guidance_strip_both_modules_and_idempotent():
    nm = _load()
    guidance = "Memory guidance.\nExample: " + _STYLE_EXAMPLE + "\nMore text."
    pb, sp = _install_agent(guidance)
    assert nm._strip_guidance_style() is True
    for mod in (pb, sp):
        assert _STYLE_EXAMPLE not in mod.MEMORY_GUIDANCE, "style example should be gone"
        assert nm._PROHIBITION in mod.MEMORY_GUIDANCE, "prohibition should be appended"
    # Idempotent across re-register.
    assert nm._strip_guidance_style() is False
    assert pb.MEMORY_GUIDANCE.count(nm._PROHIBITION) == 1
    _clear_host()


def test_noop_without_host_never_raises():
    nm = _load()
    _clear_host()  # no tools.* / agent.* installed → imports fail inside the fns
    assert nm._strip_schema_style() is False
    assert nm._strip_guidance_style() is False
    # Public entry stays quiet and returns the pair.
    assert nm.strip_native_style_capture() == (False, False)


def test_facts_survive():
    """The point of the plugin: FACT wording is untouched, only STYLE is cut."""
    nm = _load()
    schema = _install_tools("Save name, role, timezone, preferences, communication style.")
    nm._strip_schema_style()
    desc = schema["description"]
    for fact in ("name", "role", "timezone", "preferences"):
        assert fact in desc, f"fact '{fact}' must survive"
    _clear_host()


if __name__ == "__main__":
    test_schema_strip_and_idempotent()
    test_guidance_strip_both_modules_and_idempotent()
    test_noop_without_host_never_raises()
    test_facts_survive()
    print("native_memory: all checks passed ✓")
