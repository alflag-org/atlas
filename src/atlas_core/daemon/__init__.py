"""Foreground lifecycle for independent async applications, without an Atlas manager."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import signal
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from ._environment import directories, notify_ready

__all__ = ["DaemonContext", "DaemonMain", "run"]


@dataclass
class DaemonContext:
    """Application resources and cooperative stop state, supplied by :func:`run`.

    Call methods on the daemon event-loop thread. Directory ownership stays with
    the application or service manager; only the temporary runtime fallback is
    removed on exit. ``logger`` accepts standard logging ``extra`` fields.
    """

    name: str
    logger: logging.Logger
    state_dir: Path
    runtime_dir: Path
    stopping: asyncio.Event = field(default_factory=asyncio.Event)
    _ready: bool = field(default=False, init=False, repr=False)

    def ready(self) -> None:
        """Mark successful initialization, sending READY=1 when configured.

        Repeated calls are harmless. A first call after stopping is an error.
        A configured but invalid/unreachable notify socket raises an exception;
        the daemon must not continue as if readiness had been delivered.
        """
        if self._ready:
            return
        if self.stopping.is_set():
            raise RuntimeError("cannot become ready while stopping")
        notify_ready()
        self._ready = True

    async def wait_for_stop(self) -> None:
        """Wait for SIGTERM, SIGINT, or an application-set ``stopping`` event."""
        await self.stopping.wait()


DaemonMain = Callable[[DaemonContext], Awaitable[None]]
"""Async application entry point; supervise required work with asyncio.TaskGroup."""


@contextmanager
def _signals(loop: asyncio.AbstractEventLoop, context: DaemonContext) -> Iterator[None]:
    previous = {}

    def stop(_number: int, _frame: object) -> None:
        loop.call_soon_threadsafe(context.stopping.set)

    try:
        for number in (signal.SIGTERM, signal.SIGINT):
            previous[number] = signal.signal(number, stop)
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


async def _lifecycle(main: DaemonMain, context: DaemonContext, timeout: float) -> int:
    async def invoke() -> None:
        result = await main(context)
        if result is not None:
            raise TypeError("daemon main must return None")

    application = asyncio.create_task(invoke(), name="daemon-main")
    stop = asyncio.create_task(context.wait_for_stop(), name="daemon-stop")
    try:
        await asyncio.wait((application, stop), return_when=asyncio.FIRST_COMPLETED)
        if not application.done():
            done, _ = await asyncio.wait((application,), timeout=timeout)
            if not done:
                context.logger.error("shutdown timed out")
                return 1
        await application
        if not context._ready:
            context.logger.error("application stopped before readiness")
            return 1
        return 0
    finally:
        context.stopping.set()
        stop.cancel()


async def _cancel_remaining(timeout: float) -> bool:
    tasks = asyncio.all_tasks() - {asyncio.current_task()}
    for task in tasks:
        task.cancel()
    if not tasks:
        return True
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    success = not pending
    for task in done:
        if not task.cancelled() and task.exception() is not None:
            success = False
    return success


def _serve(main: DaemonMain, context: DaemonContext, timeout: float) -> int:
    # asyncio.run waits indefinitely for cancellation-resistant tasks on exit.
    # Owning this loop lets the SDK bound async cleanup without a supervisor.
    loop = asyncio.new_event_loop()
    failed = False

    def unhandled(_loop: asyncio.AbstractEventLoop, _details: dict[str, object]) -> None:
        nonlocal failed
        failed = True
        context.logger.error("unhandled event-loop failure")
        context.stopping.set()

    loop.set_exception_handler(unhandled)
    status = 1
    try:
        with _signals(loop, context):
            try:
                status = loop.run_until_complete(_lifecycle(main, context, timeout))
            except asyncio.CancelledError:
                context.logger.error("application cancelled")
            except Exception as error:
                # Exception bodies and tracebacks can contain application secrets.
                context.logger.error("application failed (%s)", type(error).__name__)
            finally:
                context.stopping.set()
                if not loop.run_until_complete(_cancel_remaining(min(timeout, 1.0))):
                    context.logger.error("task cleanup failed")
                    failed = True
                closing = loop.create_task(loop.shutdown_asyncgens())
                done, _ = loop.run_until_complete(asyncio.wait((closing,), timeout=min(timeout, 1.0)))
                if not done:
                    closing.cancel()
                    context.logger.error("async generator cleanup timed out")
                    failed = True
    finally:
        loop.close()
    return 1 if failed else status


def run(main: DaemonMain, *, name: str | None = None, shutdown_timeout: float = 30.0) -> int:
    """Run an async daemon in the foreground on the main thread.

    Return 0 after readiness and clean completion (including graceful signals),
    1 for lifecycle/cleanup failure, or 2 for invalid configuration. ``name``
    defaults to ``daemon`` and must match ``[a-z][a-z0-9]*(?:-[a-z0-9]+)*``.
    ``shutdown_timeout`` is a finite positive number of seconds. After the
    cooperative grace period, cancellation and async-generator cleanup each get
    at most min(shutdown_timeout, 1) seconds. Repeated signals do not reset it.

    Calling from a running event loop or a non-main thread raises RuntimeError.
    SystemExit and other BaseException subclasses propagate after cleanup.
    Required tasks must be awaited or held in a TaskGroup. Blocking code, threads
    and subprocesses require application-owned cleanup and an external hard
    process deadline; an async SDK cannot forcibly terminate them safely.
    """
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("daemon run requires the main thread")
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("daemon run cannot nest an event loop")

    selected_name = "daemon" if name is None else name
    logger = logging.Logger("atlas_core.daemon", level=logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(name)s: %(levelname)s: %(message)s"))
    logger.addHandler(handler)
    try:
        if not isinstance(selected_name, str) or not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", selected_name):
            raise ValueError("invalid daemon name")
        if (isinstance(shutdown_timeout, bool) or not isinstance(shutdown_timeout, (int, float))
                or not math.isfinite(shutdown_timeout) or shutdown_timeout <= 0):
            raise ValueError("invalid shutdown timeout")
        if not callable(main):
            raise TypeError("daemon main must be callable")
        logger.name = selected_name
        with directories(selected_name) as (state, runtime):
            return _serve(main, DaemonContext(selected_name, logger, state, runtime), shutdown_timeout)
    except (OSError, ValueError, TypeError) as error:
        logger.error("daemon configuration failed (%s)", type(error).__name__)
        return 2
    finally:
        logger.removeHandler(handler)
        handler.close()
