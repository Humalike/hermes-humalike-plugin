"""Checks for the SOUL.md persona helpers.

soul/__init__.py has no relative imports, so we load it straight from its file
path — the plugin's __init__.py only imports under the Hermes loader, which would
break pytest's package collection. Run directly:  python3 tests/test_soul.py
(or via pytest from inside tests/:  cd tests && pytest)
"""

import importlib.util
import json
import logging
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

_spec = importlib.util.spec_from_file_location(
    "tt_soul", Path(__file__).resolve().parent.parent / "soul" / "__init__.py"
)
soul = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = soul
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


def test_auto_state_is_per_resolved_soul_path():
    with TemporaryDirectory() as directory:
        original = soul._HERMES_HOME
        try:
            soul._HERMES_HOME = Path(directory)
            first = Path(directory) / "one" / "SOUL.md"
            second = Path(directory) / "two" / "SOUL.md"
            assert soul._auto_state_path(first) != soul._auto_state_path(second)
        finally:
            soul._HERMES_HOME = original


def test_auto_enhance_claims_once_and_marks_failure():
    class ImmediateThread:
        starts = 0

        def __init__(self, *, target, daemon):
            self.target = target

        def start(self):
            type(self).starts += 1
            self.target()

    async def unavailable(_persona):
        return None

    messages = []

    class Capture(logging.Handler):
        def emit(self, record):
            messages.append(record.getMessage())

    with TemporaryDirectory() as directory:
        original_home = soul._HERMES_HOME
        original_thread = soul.threading.Thread
        original_enhance = soul.enhance
        original_path = os.environ.get("HERMES_SOUL_PATH")
        original_log_level = soul._log.level
        handler = Capture()
        try:
            soul._HERMES_HOME = Path(directory)
            persona_path = Path(directory) / "SOUL.md"
            persona_path.write_text(REAL)
            os.environ["HERMES_SOUL_PATH"] = str(persona_path)
            soul.threading.Thread = ImmediateThread
            soul.enhance = unavailable
            soul._log.setLevel(logging.INFO)
            soul._log.addHandler(handler)
            soul.maybe_auto_enhance()
            soul.maybe_auto_enhance()
            marker = soul._auto_state_path(persona_path.resolve())
            assert ImmediateThread.starts == 1
            assert json.loads(marker.read_text())["status"] == "failed"
            assert any("use /soul enhance to retry" in message for message in messages)
        finally:
            soul._HERMES_HOME = original_home
            soul.threading.Thread = original_thread
            soul.enhance = original_enhance
            soul._log.removeHandler(handler)
            soul._log.setLevel(original_log_level)
            if original_path is None:
                os.environ.pop("HERMES_SOUL_PATH", None)
            else:
                os.environ["HERMES_SOUL_PATH"] = original_path


def test_template_does_not_claim_auto_enhance_state():
    with TemporaryDirectory() as directory:
        original_home = soul._HERMES_HOME
        original_path = os.environ.get("HERMES_SOUL_PATH")
        try:
            soul._HERMES_HOME = Path(directory)
            persona_path = Path(directory) / "SOUL.md"
            persona_path.write_text(TEMPLATE)
            os.environ["HERMES_SOUL_PATH"] = str(persona_path)
            soul.maybe_auto_enhance()
            assert not soul._auto_state_path(persona_path.resolve()).exists()
        finally:
            soul._HERMES_HOME = original_home
            if original_path is None:
                os.environ.pop("HERMES_SOUL_PATH", None)
            else:
                os.environ["HERMES_SOUL_PATH"] = original_path


def test_pending_auto_enhance_requests_manual_retry():
    with TemporaryDirectory() as directory:
        original_home = soul._HERMES_HOME
        original_path = os.environ.get("HERMES_SOUL_PATH")
        try:
            soul._HERMES_HOME = Path(directory)
            persona_path = Path(directory) / "SOUL.md"
            persona_path.write_text(REAL)
            os.environ["HERMES_SOUL_PATH"] = str(persona_path)
            marker = soul._auto_state_path(persona_path.resolve())
            marker.parent.mkdir(parents=True)
            marker.write_text(soul._auto_state("pending"))
            assert soul.maybe_auto_enhance() == (
                "⚠️ Persona auto-enhancement is still pending for SOUL.md — "
                "use /soul enhance to retry."
            )
        finally:
            soul._HERMES_HOME = original_home
            if original_path is None:
                os.environ.pop("HERMES_SOUL_PATH", None)
            else:
                os.environ["HERMES_SOUL_PATH"] = original_path


def test_unavailable_auto_state_does_not_abort_startup():
    original_claim = soul._claim_auto_state
    try:
        def unavailable(_marker):
            raise OSError("read-only filesystem")

        soul._claim_auto_state = unavailable
        with TemporaryDirectory() as directory:
            persona_path = Path(directory) / "SOUL.md"
            persona_path.write_text(REAL)
            original_path = os.environ.get("HERMES_SOUL_PATH")
            try:
                os.environ["HERMES_SOUL_PATH"] = str(persona_path)
                assert soul.maybe_auto_enhance() is None
            finally:
                if original_path is None:
                    os.environ.pop("HERMES_SOUL_PATH", None)
                else:
                    os.environ["HERMES_SOUL_PATH"] = original_path
    finally:
        soul._claim_auto_state = original_claim


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all passed")
