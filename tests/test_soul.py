"""Checks for the SOUL.md persona helpers.

soul/__init__.py has no relative imports, so we load it straight from its file
path — the plugin's __init__.py only imports under the Hermes loader, which would
break pytest's package collection. Run directly:  python3 tests/test_soul.py
(or via pytest from inside tests/:  cd tests && pytest)
"""

import importlib.util
import os
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "tt_soul", Path(__file__).resolve().parent.parent / "soul" / "__init__.py"
)
soul = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(soul)

TEMPLATE = """# Hermes Agent Persona

<!--
This file defines the agent's personality and tone.
  - "You are a warm, playful assistant."
-->
"""

REAL = """# Hermes Agent Persona

<!-- edit me -->

You are a concise technical expert. No fluff, just facts.
"""


def test_template_has_no_seed():
    assert soul.seed_body(TEMPLATE) == ""  # heading + comment only → nothing to enhance


def test_real_persona_is_a_seed():
    assert "concise technical expert" in soul.seed_body(REAL)


def test_persona_text_drops_comments_keeps_heading():
    sent = soul._persona_text(REAL)
    assert "edit me" not in sent  # comment gone
    assert "concise technical expert" in sent
    assert "Hermes Agent Persona" in sent  # heading kept (only the seed check strips it)


def test_grounding_validates():
    os.environ["HERMES_SOUL_GROUNDING"] = "nonsense"
    assert soul._grounding() == "off"
    os.environ["HERMES_SOUL_GROUNDING"] = "research"
    assert soul._grounding() == "research"
    del os.environ["HERMES_SOUL_GROUNDING"]


def test_auto_enhance_default_on_and_disableable():
    os.environ.pop("HERMES_SOUL_AUTO_ENHANCE", None)
    assert soul._auto_enabled() is True  # default on (no config in test env)
    os.environ["HERMES_SOUL_AUTO_ENHANCE"] = "false"
    assert soul._auto_enabled() is False
    os.environ["HERMES_SOUL_AUTO_ENHANCE"] = "off"
    assert soul._auto_enabled() is False
    os.environ["HERMES_SOUL_AUTO_ENHANCE"] = "true"
    assert soul._auto_enabled() is True
    del os.environ["HERMES_SOUL_AUTO_ENHANCE"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all passed")
