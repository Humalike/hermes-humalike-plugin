"""Regression coverage for supervised realtime recovery.

Run directly:  python3 tests/test_recovery.py
"""

import asyncio
import importlib
import inspect
import sys
import threading
import types
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_pkg = types.ModuleType("humalike_recovery_test")
_pkg.__path__ = [str(_ROOT)]
sys.modules["humalike_recovery_test"] = _pkg

state = importlib.import_module("humalike_recovery_test.turn_taking.state")
notify = importlib.import_module("humalike_recovery_test.turn_taking.notify")
delivery = importlib.import_module("humalike_recovery_test.turn_taking.delivery")


class _Adapter:
    async def send(self, chat_id, content, metadata=None):
        return None


def _reset_runtime():
    state.ROUTES.clear()
    state.SESSIONS.clear()
    state.DELIVERY_TASKS.clear()
    state.DELIVERY_READY.clear()
    with notify._LOCK:
        notify._last_by_kind.clear()
        notify._active_scopes.clear()


def test_http_success_cannot_clear_live_realtime_failure():
    _reset_runtime()
    scheduled = []
    original_schedule = notify._schedule
    notify._schedule = scheduled.append
    try:
        notify.alert(ConnectionError("drop"), notify.WS_LOST, kind="ws", scope="thread-a")
        assert len(scheduled) == 1
        notify.recovered_http()
        assert len(scheduled) == 1, "an unrelated HTTP success must not announce WS recovery"
        assert notify.is_active("ws", "thread-a")
        notify.recovered(kind="ws", scope="thread-a")
        assert len(scheduled) == 2
        assert not notify.is_active("ws", "thread-a")
    finally:
        notify._schedule = original_schedule


def test_http_recovery_does_not_announce_turn_taking_while_realtime_is_still_failed():
    _reset_runtime()
    scheduled = []
    original_schedule = notify._schedule
    notify._schedule = scheduled.append
    try:
        notify.alert(ConnectionError("ws drop"), notify.WS_LOST, kind="ws", scope="thread-a")
        notify.alert(ConnectionError("http outage"), kind="unreachable")
        assert len(scheduled) == 2

        notify.recovered_http()

        assert len(scheduled) == 2, "HTTP recovery must not claim turn-taking is active while WS is down"
        assert notify.is_active("ws", "thread-a")
        assert not notify.is_active("unreachable")
    finally:
        notify._schedule = original_schedule


def test_concurrent_thread_alerts_are_deduplicated_until_every_scope_recovers():
    _reset_runtime()
    scheduled = []
    schedule_lock = threading.Lock()
    original_schedule = notify._schedule

    def capture(make_coro):
        with schedule_lock:
            scheduled.append(make_coro)

    notify._schedule = capture
    try:
        scopes = [f"thread-{i}" for i in range(32)]
        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(
                lambda scope: notify.alert(
                    ConnectionError("drop"), notify.WS_LOST, kind="ws", scope=scope
                ),
                scopes,
            ))
        assert len(scheduled) == 1, "concurrent WS losses should emit one owner alert"
        assert all(notify.is_active("ws", scope) for scope in scopes)

        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(lambda scope: notify.recovered(kind="ws", scope=scope), scopes[:-1]))
        assert len(scheduled) == 1, "partial recovery must not claim realtime is restored"
        assert notify.is_active("ws", scopes[-1])

        notify.recovered(kind="ws", scope=scopes[-1])
        assert len(scheduled) == 2, "the last recovered connection emits one genuine recovery"
    finally:
        notify._schedule = original_schedule


@pytest.mark.asyncio
async def test_disconnect_reconnects_with_fresh_token_and_delivery_stays_active():
    _reset_runtime()
    adapter = _Adapter()
    attempts = []
    delivered = []
    second_connected = asyncio.Event()
    hold_second = asyncio.Event()

    original_receive = delivery._receive_loop
    original_open = delivery.open_thread
    original_sleep = delivery._sleep
    original_forward = delivery._forward

    async def fake_forward(thread_id, content, metadata=None):
        delivered.append((thread_id, content))

    async def fake_receive(url, on_message, on_typing, on_connected=None):
        attempts.append(url)
        if on_connected is not None:
            await on_connected()
        if url == "ws://initial-token":
            raise ConnectionError("induced disconnect")
        await on_message("thread-a", "after reconnect", None)
        second_connected.set()
        await hold_second.wait()

    async def fake_open(thread_id=None):
        assert thread_id == "thread-a"
        return {
            "thread": {"id": "thread-a"},
            "realtime": {"connect_url": "ws://fresh-token"},
        }

    async def no_wait(_delay):
        return None

    delivery._receive_loop = fake_receive
    delivery.open_thread = fake_open
    delivery._sleep = no_wait
    delivery._forward = fake_forward
    try:
        tid = await delivery._start_delivery(
            adapter,
            "chat-a",
            open_response={
                "thread": {"id": "thread-a"},
                "realtime": {"connect_url": "ws://initial-token"},
            },
        )
        assert tid == "thread-a"
        await asyncio.wait_for(second_connected.wait(), timeout=1)
        assert attempts == ["ws://initial-token", "ws://fresh-token"]
        assert delivered == [("thread-a", "after reconnect")]
        assert state.DELIVERY_READY == {"thread-a"}
        assert not delivery._chat_for_session("missing")

        state.SESSIONS["session-a"] = "thread-a"
        assert delivery._chat_for_session("session-a") == "chat-a"

        await delivery._stop_delivery("thread-a")
        assert "thread-a" not in state.DELIVERY_TASKS
        assert "thread-a" not in state.DELIVERY_READY
        await asyncio.sleep(0)
        assert attempts == ["ws://initial-token", "ws://fresh-token"], "shutdown must not reconnect"
    finally:
        hold_second.set()
        delivery._receive_loop = original_receive
        delivery.open_thread = original_open
        delivery._sleep = original_sleep
        delivery._forward = original_forward
        await delivery._stop_all_deliveries()


@pytest.mark.asyncio
async def test_multiple_threads_have_independent_supervisors_and_clean_shutdown():
    _reset_runtime()
    adapter = _Adapter()
    connected = Counter()
    both_ready = asyncio.Event()
    blockers = {"thread-a": asyncio.Event(), "thread-b": asyncio.Event()}

    original_receive = delivery._receive_loop

    async def fake_receive(url, on_message, on_typing, on_connected=None):
        tid = url.rsplit("/", 1)[-1]
        connected[tid] += 1
        if on_connected is not None:
            await on_connected()
        if sum(connected.values()) == 2:
            both_ready.set()
        await blockers[tid].wait()

    delivery._receive_loop = fake_receive
    try:
        for tid in ("thread-a", "thread-b"):
            result = await delivery._start_delivery(
                adapter,
                f"chat-{tid[-1]}",
                open_response={
                    "thread": {"id": tid},
                    "realtime": {"connect_url": f"ws://token/{tid}"},
                },
            )
            assert result == tid
        await asyncio.wait_for(both_ready.wait(), timeout=1)
        assert set(state.DELIVERY_TASKS) == {"thread-a", "thread-b"}
        assert state.DELIVERY_READY == {"thread-a", "thread-b"}
        assert state.DELIVERY_TASKS["thread-a"] is not state.DELIVERY_TASKS["thread-b"]

        tasks = list(state.DELIVERY_TASKS.values())
        await delivery._stop_all_deliveries()
        assert not state.DELIVERY_TASKS
        assert not state.DELIVERY_READY
        assert all(task.done() for task in tasks)
    finally:
        for blocker in blockers.values():
            blocker.set()
        delivery._receive_loop = original_receive
        await delivery._stop_all_deliveries()


@pytest.mark.asyncio
async def test_reconnect_grant_failures_keep_retrying_with_bounded_backoff():
    _reset_runtime()
    adapter = _Adapter()
    sleeps = []
    grant_attempts = 0
    reconnected = asyncio.Event()
    hold = asyncio.Event()

    original_receive = delivery._receive_loop
    original_open = delivery.open_thread
    original_sleep = delivery._sleep

    async def fake_receive(url, on_message, on_typing, on_connected=None):
        if url == "ws://initial":
            raise ConnectionError("drop")
        if on_connected is not None:
            await on_connected()
        reconnected.set()
        await hold.wait()

    async def fake_open(thread_id=None):
        nonlocal grant_attempts
        grant_attempts += 1
        if grant_attempts == 1:
            return None
        return {
            "thread": {"id": thread_id},
            "realtime": {"connect_url": "ws://fresh"},
        }

    async def record_sleep(delay):
        sleeps.append(delay)

    delivery._receive_loop = fake_receive
    delivery.open_thread = fake_open
    delivery._sleep = record_sleep
    try:
        await delivery._start_delivery(
            adapter,
            "chat-a",
            open_response={
                "thread": {"id": "thread-a"},
                "realtime": {"connect_url": "ws://initial"},
            },
        )
        await asyncio.wait_for(reconnected.wait(), timeout=1)
        assert grant_attempts == 2
        assert sleeps == [1.0, 2.0]
    finally:
        hold.set()
        delivery._receive_loop = original_receive
        delivery.open_thread = original_open
        delivery._sleep = original_sleep
        await delivery._stop_all_deliveries()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        if inspect.iscoroutinefunction(fn):
            asyncio.run(fn())
        else:
            fn()
        print(f"ok {name}")
    print("all passed")
