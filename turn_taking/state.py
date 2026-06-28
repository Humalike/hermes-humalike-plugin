"""Shared mutable runtime state for the turn-taking plugin.

One home for every cross-module value, so readers and writers in different
modules never drift onto separate copies — a plain ``global`` only rebinds the
name in its own module, so the moment this state is split across files it MUST be
referenced as ``state.NAME`` everywhere. No logic lives here: only the maps, the
contextvar, the captured loop, and the open-lock the pipeline threads through.

These names are this module's public surface (accessed as ``state.NAME`` from the
sibling modules), so they carry no leading underscore.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from typing import Any, Callable, Dict, Optional, Tuple

# ── Delivery routing ──────────────────────────────────────────────────────────
# thread_id → (adapter, chat_id): where to deliver this thread's bubbles.
ROUTES: Dict[str, Tuple[Any, str]] = {}

# The genuine adapter.send per adapter class, captured before patching, so
# _forward delivers bubbles via the ORIGINAL — they bypass our patch entirely,
# which is what suppresses the agent's monolithic draft. No metadata marker needed.
# Keyed by adapter class so WhatsApp and Telegram can be patched at once without a
# single global clobbering one platform's original with the other's.
ORIG_SEND: Dict[type, Callable] = {}

# The genuine adapter.handle_message per adapter class, captured before patching
# handle_message to gate media. Only platforms whose handle_message is gated have
# an entry; _handle_inbound dispatches a "speak" turn through this genuine handler
# (so it doesn't recurse into the gate). No entry (e.g. WhatsApp, whose media goes
# through the patched _poll loop, not a gated handle_message) → _handle_inbound
# falls back to the adapter's own (unpatched) handle_message.
ORIG_HANDLE: Dict[type, Callable] = {}

# ── Session → thread map: one thread per conversation ─────────────────────────
SESSIONS: Dict[str, str] = {}  # Hermes session_id → turn-taking thread_id
OPEN_LOCK = asyncio.Lock()     # serializes thread-opens (rare) so a burst can't double-open

# ── Decide gate: per-turn epoch ───────────────────────────────────────────────
# Per-turn epoch keyed by the platform message_id of the message that turn answers.
# Keying by message_id — not by a single per-session slot — binds each draft
# to its OWN epoch at respond time: the transform hook recovers this turn's
# message_id from Hermes's per-task session contextvar (HERMES_SESSION_MESSAGE_ID,
# set by the gateway before the turn runs and propagated into the worker thread via
# copy_context). That is ordering-independent and needs no interrupt to stay correct.
EPOCH_BY_MESSAGE_ID: Dict[str, int] = {}  # message_id → turn_epoch of the "speak" decision

# Per-turn RAW message_id, carried into the turn's worker thread and read by the
# transform hook. Bound as a side effect of GatewayRunner._reply_anchor_for_event
# (see _patch__reply_anchor_for_event), NOT in the inbound task: a queued message's
# turn runs via _run_agent's recursive follow-up (run.py:19091) INSIDE the prior
# turn's task, so an inbound-task set would never reach it. Both turn paths compute
# the reply anchor from their event right before _run_agent (first run.py:9460,
# queued run.py:19075), each with the raw event in scope — so binding event.message_id
# there, before copy_context() snapshots it for the worker thread, gives every turn
# (first or queued) its correct id. We bind event.message_id (NOT the anchor return
# value, which is None on Telegram topics) — the same id the decide side keys by.
# (Hermes's own HERMES_SESSION_MESSAGE_ID is empty on the WhatsApp path, hence ours.)
TT_MID_CTX: ContextVar[str] = ContextVar("tt_message_id", default="")

# The gateway's asyncio loop, captured on the inbound path (which runs in it).
# transform_llm_output fires in the agent's worker thread where there is NO running
# loop, so _respond must be scheduled onto this loop via run_coroutine_threadsafe —
# create_task there raises "no running event loop" and the naturalize call is lost.
LOOP = None

# ── Send suppression ──────────────────────────────────────────────────────────
# chat_id → {exact answer strings awaiting their raw send}. transform_llm_output
# registers the FINAL answer's text here; the send patch drops the send whose
# content matches (it's naturalized + delivered over WS instead). Keying by
# CONTENT (not "next send") makes suppression order-independent: it targets the
# answer by identity, so a racing tool-notice / error send can't consume it.
PENDING_ANSWERS: Dict[str, set] = {}
