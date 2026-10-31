from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import sys
import threading
from pathlib import Path

import pytest

from atlas_core.daemon import DaemonContext, run
from atlas_core.daemon import _environment as environment


@pytest.fixture(autouse=True)
def daemon_environment(monkeypatch, tmp_path):
    for key in ("STATE_DIRECTORY", "RUNTIME_DIRECTORY", "NOTIFY_SOCKET", "XDG_RUNTIME_DIR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


async def ready(ctx):
    ctx.ready()


def test_standalone_readiness_logging_and_directory_lifetime(capsys):
    observed = []

    async def main(ctx):
        assert ctx.name == "daemon"
        assert isinstance(ctx.logger, logging.Logger)
        assert ctx.state_dir.is_dir() and ctx.runtime_dir.is_dir()
        assert ctx.runtime_dir.stat().st_mode & 0o777 == 0o700
        observed.extend((ctx.state_dir, ctx.runtime_dir))
        ctx.logger.info("application initialized", extra={"phase": "startup"})
        ctx.ready()
        ctx.ready()

    assert run(main) == 0
    state, runtime = observed
    assert state.exists() and not runtime.exists()
    output = capsys.readouterr()
    assert not output.out
    assert "application initialized" in output.err


@pytest.mark.parametrize("number", [signal.SIGINT, signal.SIGTERM])
def test_signals_stop_and_cleanup_without_changing_handlers(number):
    original = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    events = []

    async def main(ctx):
        events.append("start")
        ctx.ready()
        events.append("ready")
        asyncio.get_running_loop().call_soon(os.kill, os.getpid(), number)
        await ctx.wait_for_stop()
        events.append("stop")
        os.kill(os.getpid(), number)
        await asyncio.sleep(0)
        events.append("cleanup")

    assert run(main, name="hoged", shutdown_timeout=0.1) == 0
    assert events == ["start", "ready", "stop", "cleanup"]
    assert original == {sig: signal.getsignal(sig) for sig in original}


@pytest.mark.parametrize("mode", ["startup", "missing", "crash", "child", "cancel", "result"])
def test_application_failures_are_nonzero_without_secret_bodies(mode, capsys):
    async def crash():
        raise RuntimeError("synthetic-secret-value")

    async def main(ctx):
        if mode == "startup":
            await crash()
        if mode == "missing":
            return
        ctx.ready()
        if mode == "crash":
            await crash()
        if mode == "child":
            async with asyncio.TaskGroup() as group:
                group.create_task(crash())
                await ctx.wait_for_stop()
        if mode == "cancel":
            raise asyncio.CancelledError
        if mode == "result":
            return 12

    assert run(main) == 1
    output = capsys.readouterr()
    assert not output.out
    assert "synthetic-secret-value" not in output.err
    assert "ERROR" in output.err


def test_timeout_cancels_and_awaits_cleanup(capsys):
    cleaned = []

    async def main(ctx):
        ctx.ready()
        ctx.stopping.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.append(True)

    assert run(main, shutdown_timeout=0.02) == 1
    assert cleaned == [True]
    assert "shutdown timed out" in capsys.readouterr().err


def test_cleanup_failure_and_unhandled_callback(capsys):
    async def main(ctx):
        async def child():
            try:
                await asyncio.Event().wait()
            finally:
                raise ValueError("synthetic-secret-value")

        task = asyncio.create_task(child())
        await asyncio.sleep(0)
        ctx.ready()
        # Leaving work outside a TaskGroup still cannot hide cleanup failures.
        assert not task.done()

    assert run(main) == 1

    async def callback_main(ctx):
        def fail():
            raise ValueError("synthetic-secret-value")

        ctx.ready()
        asyncio.get_running_loop().call_soon(fail)
        await ctx.wait_for_stop()

    assert run(callback_main) == 1
    output = capsys.readouterr().err
    assert "task cleanup failed" in output
    assert "unhandled event-loop failure" in output
    assert "synthetic-secret-value" not in output


def test_stop_before_readiness_and_repeated_ready_after_stop():
    async def main(ctx):
        ctx.stopping.set()
        ctx.ready()

    assert run(main) == 1

    async def initialized(ctx):
        ctx.ready()
        ctx.stopping.set()
        ctx.ready()

    assert run(initialized) == 0


@pytest.mark.parametrize("address_type", ["path", "abstract"])
def test_notify_once_only_after_initialization(tmp_path, monkeypatch, address_type):
    address = str(tmp_path / "notify") if address_type == "path" else f"\0atlas-test-{os.getpid()}"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
        receiver.bind(address)
        receiver.settimeout(0.05)
        monkeypatch.setenv("NOTIFY_SOCKET", address.replace("\0", "@"))

        async def fail(_ctx):
            raise RuntimeError("initialization failed")

        assert run(fail) == 1
        with pytest.raises(TimeoutError):
            receiver.recv(128)

        async def main(ctx):
            ctx.ready()
            ctx.ready()

        assert run(main) == 0
        assert receiver.recv(128) == b"READY=1"
        with pytest.raises(TimeoutError):
            receiver.recv(128)


@pytest.mark.parametrize("address", ["", "relative", "@", "/", "/missing/notify"])
def test_invalid_or_unreachable_notify_fails(monkeypatch, address):
    monkeypatch.setenv("NOTIFY_SOCKET", address)
    assert run(ready) == 1


def test_notify_queue_failure_is_not_reported_ready(monkeypatch, tmp_path):
    monkeypatch.setenv("NOTIFY_SOCKET", str(tmp_path / "notify"))

    class FullSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def setblocking(self, flag):
            assert not flag

        def sendto(self, *_args):
            raise BlockingIOError

    monkeypatch.setattr(environment.socket, "socket", lambda *_args: FullSocket())
    context = DaemonContext("test", logging.Logger("test"), tmp_path, tmp_path)
    with pytest.raises(BlockingIOError):
        context.ready()
    assert not context._ready


def test_manager_and_xdg_directories(monkeypatch, tmp_path):
    state, runtime = tmp_path / "managed-state", tmp_path / "managed-runtime"
    monkeypatch.setenv("STATE_DIRECTORY", str(state))
    monkeypatch.setenv("RUNTIME_DIRECTORY", str(runtime))

    async def main(ctx):
        assert (ctx.state_dir, ctx.runtime_dir) == (state, runtime)
        ctx.ready()

    assert run(main) == 0
    assert state.exists() and runtime.exists()
    monkeypatch.delenv("STATE_DIRECTORY")
    monkeypatch.delenv("RUNTIME_DIRECTORY")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg-runtime"))
    with environment.directories("hoged") as (state, runtime):
        assert state == tmp_path / "state/atlas/hoged"
        assert runtime == tmp_path / "xdg-runtime/atlas/hoged"
    assert runtime.exists()
    monkeypatch.delenv("XDG_STATE_HOME")
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    with environment.directories("hoged") as (state, _runtime):
        assert state == tmp_path / "home/.local/state/atlas/hoged"


@pytest.mark.parametrize("key,value", [
    ("STATE_DIRECTORY", ""), ("STATE_DIRECTORY", "/one:/two"),
    ("RUNTIME_DIRECTORY", "relative"), ("XDG_STATE_HOME", "relative"),
    ("XDG_RUNTIME_DIR", "/tmp/../unsafe"),
])
def test_invalid_directory_environment(monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    assert run(ready) == 2


@pytest.mark.parametrize("kind", ["symlink", "file", "permissions", "owner", "readonly"])
def test_unsafe_directories_fail_before_application(monkeypatch, tmp_path, kind):
    path = tmp_path / "unsafe"
    if kind == "file":
        path.write_text("not a directory")
    elif kind == "symlink":
        target = tmp_path / "target"
        target.mkdir()
        path.symlink_to(target, target_is_directory=True)
        path = path / "must-not-create"
    else:
        path.mkdir(mode=0o700)
        if kind == "permissions":
            path.chmod(0o755)
        elif kind == "owner":
            monkeypatch.setattr(environment.os, "getuid", lambda: -1)
        else:
            monkeypatch.setattr(environment.os, "access", lambda *_args: False)
    monkeypatch.setenv("STATE_DIRECTORY", str(path))
    assert run(ready) == 2
    if kind == "symlink":
        assert not (target / "must-not-create").exists()


@pytest.mark.parametrize("kwargs", [
    {"name": "../escape"}, {"name": ""}, {"name": 1},
    {"shutdown_timeout": 0}, {"shutdown_timeout": -1}, {"shutdown_timeout": True},
    {"shutdown_timeout": "1"}, {"shutdown_timeout": float("inf")},
    {"shutdown_timeout": float("nan")},
])
def test_invalid_options(kwargs):
    assert run(ready, **kwargs) == 2


def test_invalid_callable():
    assert run(None) == 2


def test_run_rejects_nested_loop_and_worker_thread():
    async def nested():
        with pytest.raises(RuntimeError, match="nest"):
            run(ready)

    asyncio.run(nested())
    errors = []

    def worker():
        try:
            run(ready)
        except RuntimeError as error:
            errors.append(str(error))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert errors == ["daemon run requires the main thread"]


def test_baseexception_propagates_and_handlers_are_restored():
    class StopNow(BaseException):
        pass

    async def main(ctx):
        ctx.ready()
        raise StopNow

    original = signal.getsignal(signal.SIGTERM)
    with pytest.raises(StopNow):
        run(main)
    assert signal.getsignal(signal.SIGTERM) is original


def test_no_private_atlas_or_cluster_imports():
    import subprocess

    result = subprocess.run(
        [sys.executable, "-c", "import sys; import atlas_core.daemon; "
         "assert not any(n == 'atlas' or n.startswith(('atlas.', 'atlas_core.cluster')) for n in sys.modules)"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_cooperative_cleanup_finishes_within_grace():
    async def main(ctx):
        ctx.ready()
        ctx.stopping.set()
        await asyncio.sleep(0.01)

    assert run(main, shutdown_timeout=0.2) == 0


def test_async_generator_cleanup_is_bounded(capsys):
    generators = []

    async def resource():
        try:
            yield
        finally:
            await asyncio.Event().wait()

    async def main(ctx):
        generator = resource()
        generators.append(generator)
        await anext(generator)
        ctx.ready()

    assert run(main, shutdown_timeout=0.02) == 1
    assert "async generator cleanup timed out" in capsys.readouterr().err


def test_task_suppressing_cancellation_cannot_hang_run(capsys):
    tasks = []

    async def main(ctx):
        tasks.append(asyncio.current_task())
        ctx.ready()
        ctx.stopping.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.Event().wait()

    assert run(main, shutdown_timeout=0.02) == 1
    assert "task cleanup failed" in capsys.readouterr().err
    assert tasks[0].get_loop().is_closed()


def test_filesystem_denial_does_not_start_application(monkeypatch, capsys):
    def readonly(*_args, **_kwargs):
        raise OSError(30, "synthetic-secret-value")

    monkeypatch.setattr(Path, "mkdir", readonly)
    assert run(ready) == 2
    assert "synthetic-secret-value" not in capsys.readouterr().err
