"""Focused checks for supervised realtime recovery.

Run directly: python3 tests/test_recovery.py
"""

import asyncio
import importlib
import inspect
import sys
import types
from pathlib import Path

try:
    import httpx as _httpx  # noqa: F401
except ImportError:
    _httpx_stub = types.SimpleNamespace(
        AsyncClient=object,
        HTTPError=type("HTTPError", (Exception,), {}),
        HTTPStatusError=type("HTTPStatusError", (Exception,), {}),
    )
    sys.modules["httpx"] = _httpx_stub
else:
    _httpx_stub = None

_ROOT = Path(__file__).resolve().parent.parent
_pkg = types.ModuleType("humalike_recovery_test")
_pkg.__path__ = [str(_ROOT)]
sys.modules["humalike_recovery_test"] = _pkg

state = importlib.import_module("humalike_recovery_test.turn_taking.state")
notify = importlib.import_module("humalike_recovery_test.turn_taking.notify")
service = importlib.import_module("humalike_recovery_test.turn_taking.service")
delivery = importlib.import_module("humalike_recovery_test.turn_taking.delivery")

if _httpx_stub is not None and sys.modules.get("httpx") is _httpx_stub:
    del sys.modules["httpx"]


class _Adapter:
    async def send(self, chat_id, content, metadata=None):
        pass


def _reset():
    state.ROUTES.clear()
    state.SESSIONS.clear()
    state.DELIVERY_TASKS.clear()
    with notify._LOCK:
        notify._last_by_kind.clear()
        notify._active_scopes.clear()
        notify._announced_kinds.clear()


def test_recovery_waits_for_every_http_and_websocket_failure():
    _reset()
    scheduled = []
    original = notify._schedule
    notify._schedule = scheduled.append
    try:
        notify.alert(ConnectionError(), notify.WS_LOST, kind="ws", scope="thread-a")
        notify.alert(ConnectionError(), notify.WS_LOST, kind="ws", scope="thread-b")
        notify.alert(ConnectionError(), kind="unreachable")
        assert len(scheduled) == 2  # one WS alert plus one HTTP alert

        notify.recovered(kind="ws", scope="thread-a")
        notify.recovered_http()
        assert len(scheduled) == 2
        assert notify.is_active("ws", "thread-b")

        notify.recovered(kind="ws", scope="thread-b")
        assert len(scheduled) == 3  # one recovery, after the final fault clears
    finally:
        notify._schedule = original


async def test_reconnect_uses_fresh_token_backoff_and_stops_when_cancelled():
    _reset()
    attempts, sleeps = [], []
    grants = 0
    connected = asyncio.Event()
    hold = asyncio.Event()
    original_receive = delivery._receive_loop
    original_open = delivery.open_thread
    original_sleep = delivery._sleep

    async def receive(url, _on_message, _on_typing, on_connected=None):
        attempts.append(url)
        if url == "ws://initial":
            raise ConnectionError("drop")
        await on_connected()
        connected.set()
        await hold.wait()

    async def reopen(thread_id=None):
        nonlocal grants
        grants += 1
        if grants == 1:
            return None
        return {
            "thread": {"id": thread_id},
            "realtime": {"connect_url": "ws://fresh"},
        }

    async def sleep(delay):
        sleeps.append(delay)

    delivery._receive_loop = receive
    delivery.open_thread = reopen
    delivery._sleep = sleep
    try:
        await delivery._start_delivery(
            _Adapter(),
            "chat-a",
            open_response={
                "thread": {"id": "thread-a"},
                "realtime": {"connect_url": "ws://initial"},
            },
        )
        await asyncio.wait_for(connected.wait(), 1)
        assert attempts == ["ws://initial", "ws://fresh"]
        assert sleeps == [1.0, 2.0]

        await delivery._stop_delivery("thread-a")
        await asyncio.sleep(0)
        assert attempts == ["ws://initial", "ws://fresh"]
        assert "thread-a" not in state.DELIVERY_TASKS
    finally:
        hold.set()
        delivery._receive_loop = original_receive
        delivery.open_thread = original_open
        delivery._sleep = original_sleep
        await delivery._stop_all_deliveries()


async def test_permanent_dependency_failure_does_not_retry():
    _reset()
    grants = sleeps = 0
    alerts = []
    original_receive = delivery._receive_loop
    original_open = delivery.open_thread
    original_sleep = delivery._sleep
    original_alert = notify.alert
    original_schedule = notify._schedule

    async def receive(*_args, **_kwargs):
        raise service.WebSocketDependencyError("missing")

    async def reopen(_thread_id=None):
        nonlocal grants
        grants += 1

    async def sleep(_delay):
        nonlocal sleeps
        sleeps += 1

    def alert(*args, **kwargs):
        alerts.append((args, kwargs))
        original_alert(*args, **kwargs)

    delivery._receive_loop = receive
    delivery.open_thread = reopen
    delivery._sleep = sleep
    notify.alert = alert
    notify._schedule = lambda _make_coro: None
    try:
        await delivery._start_delivery(
            _Adapter(),
            "chat-a",
            open_response={
                "thread": {"id": "thread-a"},
                "realtime": {"connect_url": "ws://initial"},
            },
        )
        await asyncio.wait_for(state.DELIVERY_TASKS["thread-a"], 1)
        assert grants == sleeps == 0
        assert "thread-a" not in state.DELIVERY_TASKS
        assert notify.is_active("ws", "thread-a")
        assert "reconnecting" not in alerts[0][0][1]
    finally:
        delivery._receive_loop = original_receive
        delivery.open_thread = original_open
        delivery._sleep = original_sleep
        notify.alert = original_alert
        notify._schedule = original_schedule
        await delivery._stop_all_deliveries()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            asyncio.run(fn()) if inspect.iscoroutinefunction(fn) else fn()
            print(f"ok {name}")
