"""Threading glue: an asyncio loop on a worker thread, and a way back onto the Qt main thread.

All model calls, websockets and subprocesses run on the asyncio loop so the UI never stalls.
UI updates must happen on the Qt thread, so services call `ui(fn, *args)` to queue them there,
and `await ui_call(fn)` when they need the return value or to wait for an animation to finish.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from typing import Any, TypeVar

from PyQt6.QtCore import QObject, Qt, pyqtSignal, pyqtSlot

log = logging.getLogger(__name__)
T = TypeVar("T")


class AsyncRunner:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="nimbus-async", daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def start(self) -> None:
        self._thread.start()

    def submit(self, coro: Awaitable[T]) -> Future[T]:
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)  # type: ignore[arg-type]
        future.add_done_callback(_log_failure)
        return future

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)


def _log_failure(future: Future) -> None:
    if future.cancelled():
        return
    exc = future.exception()
    if exc is not None:
        log.error("background task failed", exc_info=exc)


class _Bridge(QObject):
    invoke = pyqtSignal(object)

    def __init__(self) -> None:
        super().__init__()
        self.invoke.connect(self._run, Qt.ConnectionType.QueuedConnection)

    @pyqtSlot(object)
    def _run(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:
            log.exception("UI callback failed")


_bridge: _Bridge | None = None


def install_bridge() -> None:
    """Create the bridge on the Qt main thread (call once, after QApplication exists)."""
    global _bridge
    _bridge = _Bridge()


def ui(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Run `fn` on the Qt main thread, fire-and-forget."""
    if _bridge is None:
        raise RuntimeError("install_bridge() was not called")
    _bridge.invoke.emit(lambda: fn(*args, **kwargs))


async def ui_call(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run `fn` on the Qt main thread and await its result."""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[T] = loop.create_future()

    def run() -> None:
        try:
            result = fn(*args, **kwargs)
        except BaseException as exc:  # forwarded to the awaiting coroutine
            loop.call_soon_threadsafe(_set_exc, fut, exc)
        else:
            loop.call_soon_threadsafe(_set_result, fut, result)

    ui(run)
    return await fut


async def ui_wait(start: Callable[[Callable[[], None]], None], timeout: float = 10.0) -> None:
    """Start something on the UI thread that reports completion through a callback; await it.

    Used for buddy flights: `await ui_wait(lambda done: overlay.fly_to(x, y, on_land=done))`.
    """
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[None] = loop.create_future()

    def done() -> None:
        loop.call_soon_threadsafe(_set_result, fut, None)

    ui(start, done)
    try:
        await asyncio.wait_for(fut, timeout)
    except asyncio.TimeoutError:
        log.warning("UI operation did not complete within %.1fs", timeout)


def _set_result(fut: asyncio.Future, value: Any) -> None:
    if not fut.done():
        fut.set_result(value)


def _set_exc(fut: asyncio.Future, exc: BaseException) -> None:
    if not fut.done():
        fut.set_exception(exc)
