"""Stop native memory from capturing conversational *style*.

Hermes's built-in memory tool is told to record how the user/group communicates
(tone, formality, verbosity, the group's norms). The Humalike plugin already
owns that layer: the per-conversation voice card in ``social_learning/`` learns
the live style and injects it into every reply. Letting native memory ALSO
persist style double-captures it — and a stale saved snapshot then fights the
live card. So at register time we surgically remove the style clause from the
memory tool and forbid re-capturing it, leaving durable FACT memory (name, role,
timezone, stated preferences) untouched.

Two edits, both at load time, no hooks:
  1. memory tool schema description (``tools.memory_tool`` via the registry) —
     read live each request, so mutating it in place takes effect.
  2. ``MEMORY_GUIDANCE`` — imported by-name into ``agent.system_prompt`` and
     ``agent.prompt_builder``, so we patch the binding the prompt builder reads.

Best-effort and idempotent; never raises. No-op on a Hermes without these
internals (the imports simply fail and each edit reports False). The host
imports are deferred into the functions so importing this module never needs
the Hermes runtime — the register-time call is the only place it touches it.

Run the checks directly:  python3 tests/test_native_memory.py
"""

from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger(__name__)

# Explicit prohibition injected into both the tool schema and the guidance.
# The marker lets us stay idempotent (don't append twice on re-register).
_PROHIBITION = (
    "NEVER save how the user or group communicates or behaves socially. This is "
    "owned by a separate social-learning layer; recording any of it in memory is "
    "forbidden. Specifically do NOT save:\n"
    "  - STYLE: tone, register/formality (terse, warm, formal, casual), message "
    "length/verbosity, emoji/slang/punctuation/capitalization habits, formatting, "
    "or how people phrase or word things.\n"
    "  - NORMS: the group's unwritten social rules and conversational conventions "
    "— when to speak up or stay quiet, how much detail is wanted, what gets mocked "
    "or corrected, inside jokes, greetings/sign-offs, response timing/rhythm, or "
    "any 'how this group/conversation operates' behavioral pattern.\n"
    "Save only durable FACTS (name, role, timezone, stated preferences about the "
    "task itself, environment details) — never the manner of communication."
)


def _strip_schema_style() -> bool:
    """Remove the 'communication style' clause from the live memory tool schema
    AND append an explicit do-not-save prohibition. Returns True if it changed
    anything."""
    try:
        import tools.memory_tool  # noqa: F401 — ensure the tool is registered
        from tools.registry import registry

        entry = registry.get_entry("memory")
        schema = getattr(entry, "schema", None) if entry else None
        if not isinstance(schema, dict):
            return False
        desc = schema.get("description", "")
        changed = False
        if "communication style" in desc:
            desc = desc.replace(", communication style", "")
            changed = True
        if _PROHIBITION not in desc:
            # Tack the prohibition onto the existing SKIP guidance.
            desc = desc.rstrip() + "\n\n" + _PROHIBITION
            changed = True
        if changed:
            schema["description"] = desc
        return changed
    except Exception as exc:
        _log.warning("turn-taking: native-memory schema patch failed: %s", exc)
        return False


def _strip_guidance_style() -> bool:
    """Remove the style-flavored example from MEMORY_GUIDANCE in the modules that
    actually read it, and append the prohibition. Returns True if it patched a
    module."""
    patched = False
    # The 'concise responses' example is the only style-ish line in the guidance.
    style_example = (
        "'User prefers concise responses' ✓ — 'Always respond concisely' ✗. "
    )
    try:
        import agent.prompt_builder as pb
        import agent.system_prompt as sp

        for mod in (pb, sp):
            text = getattr(mod, "MEMORY_GUIDANCE", None)
            if not isinstance(text, str):
                continue
            new = text
            if style_example in new:
                new = new.replace(style_example, "")
            if _PROHIBITION not in new:
                new = new.rstrip() + "\n" + _PROHIBITION
            if new != text:
                setattr(mod, "MEMORY_GUIDANCE", new)
                patched = True
    except Exception as exc:
        _log.warning("turn-taking: native-memory guidance patch failed: %s", exc)
    return patched


def strip_native_style_capture() -> tuple[bool, bool]:
    """Apply both edits at register time. Returns ``(schema_patched,
    guidance_patched)`` for logging/tests; never raises."""
    schema = _strip_schema_style()
    guidance = _strip_guidance_style()
    _log.info(
        "turn-taking: native-memory style capture stripped (schema=%s, guidance=%s)",
        schema, guidance,
    )
    return schema, guidance
