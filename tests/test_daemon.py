from __future__ import annotations

import asyncio
import os
import signal
import socket
import sys
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from atlas_core.daemon import _environment as environment
from atlas_core.daemon import run


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
        (ctx.state_dir / "checkpoint").write_text("saved")
        (ctx.runtime_dir / "scratch").touch()
        assert ctx.runtime_dir.stat().st_mode & 0o777 == 0o700
        observed.extend((ctx.state_dir, ctx.runtime_dir, ctx.logger))
        ctx.logger.info("application initialized", extra={"phase": "startup"})
        ctx.ready()

    assert run(main) == 0
    state, runtime, logger = observed
    assert (state / "checkpoint").read_text() == "saved"
    assert not runtime.exists()
    logger.error("late message from a completed run")
    output = capsys.readouterr()
    assert not output.out
    assert "application initialized" in output.err
    assert "late message" not in output.err


def test_stop_handlers_are_idempotent_and_restored(monkeypatch):
    original = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
    handlers = dict(original)
    events = []

    def register(number, handler):
        previous = handlers[number]
        handlers[number] = handler
        return previous

    monkeypatch.setattr(signal, "signal", register)

    async def main(ctx):
        ctx.ready()
        asyncio.get_running_loop().call_soon(handlers[signal.SIGTERM], signal.SIGTERM, None)
        await ctx.wait_for_stop()
        events.append("stopping")
        handlers[signal.SIGINT](signal.SIGINT, None)
        await asyncio.sleep(0.01)
        events.append("cleaned")

    assert run(main, shutdown_timeout=0.2) == 0
    assert events == ["stopping", "cleaned"]
    assert handlers == original


@pytest.mark.parametrize("initialized", [False, True], ids=["startup", "running"])
def test_application_exception_fails_without_disclosing_secrets(initialized, capsys):
    async def main(ctx):
        if initialized:
            ctx.ready()
        raise RuntimeError("synthetic-secret-value")

    assert run(main) == 1
    output = capsys.readouterr()
    assert not output.out
    assert "synthetic-secret-value" not in output.err
    assert "application failed" in output.err


def test_return_without_readiness_is_startup_failure():
    async def main(_ctx):
        pass

    assert run(main) == 1


def test_task_group_failure_stops_application(capsys):
    async def worker():
        raise RuntimeError("synthetic-secret-value")

    async def main(ctx):
        ctx.ready()
        async with asyncio.TaskGroup() as group:
            group.create_task(worker())
            await ctx.wait_for_stop()

    assert run(main) == 1
    assert "synthetic-secret-value" not in capsys.readouterr().err


def test_cancelled_application_is_failure():
    async def main(ctx):
        ctx.ready()
        raise asyncio.CancelledError

    assert run(main) == 1


def test_application_cannot_return_a_command_exit_code():
    async def main(ctx):
        ctx.ready()
        return 12

    assert run(main) == 1


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


def test_background_task_cleanup_failure_is_not_hidden(capsys):
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

    assert "task cleanup failed" in capsys.readouterr().err


def test_unhandled_callback_stops_application_without_disclosing_secrets(capsys):
    async def main(ctx):
        def fail():
            raise ValueError("synthetic-secret-value")

        ctx.ready()
        asyncio.get_running_loop().call_soon(fail)
        await ctx.wait_for_stop()

    assert run(main) == 1
    output = capsys.readouterr().err
    assert "unhandled event-loop failure" in output
    assert "synthetic-secret-value" not in output


def test_stop_before_readiness_is_startup_failure():
    async def main(ctx):
        ctx.stopping.set()
        ctx.ready()

    assert run(main) == 1


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
            ctx.stopping.set()
            ctx.ready()

        assert run(main) == 0
        assert receiver.recv(128) == b"READY=1"
        with pytest.raises(TimeoutError):
            receiver.recv(128)


@pytest.mark.parametrize("address", ["", "relative", "@", "/missing/notify"])
def test_invalid_or_unreachable_notify_fails(monkeypatch, address):
    monkeypatch.setenv("NOTIFY_SOCKET", address)
    assert run(ready) == 1


def test_failed_notification_can_be_retried(monkeypatch, tmp_path):
    monkeypatch.setenv("NOTIFY_SOCKET", str(tmp_path / "notify"))
    notify = Mock(side_effect=[BlockingIOError, None])
    monkeypatch.setattr("atlas_core.daemon.notify_ready", notify)

    async def main(ctx):
        with pytest.raises(BlockingIOError):
            ctx.ready()
        ctx.ready()

    assert run(main) == 0
    assert notify.call_count == 2


@pytest.mark.parametrize("selection", ["manager", "xdg", "home"])
def test_selected_directories_are_retained_after_exit(monkeypatch, tmp_path, selection):
    state = tmp_path / "state/atlas/sampled"
    runtime = tmp_path / "runtime/atlas/sampled"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    if selection == "manager":
        state, runtime = tmp_path / "managed-state", tmp_path / "managed-runtime"
        monkeypatch.setenv("STATE_DIRECTORY", str(state))
        monkeypatch.setenv("RUNTIME_DIRECTORY", str(runtime))
    elif selection == "home":
        monkeypatch.delenv("XDG_STATE_HOME")
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        state = tmp_path / "home/.local/state/atlas/sampled"

    async def main(ctx):
        assert (ctx.state_dir, ctx.runtime_dir) == (state, runtime)
        ctx.ready()

    assert run(main, name="sampled") == 0
    assert state.exists() and runtime.exists()


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
    {"shutdown_timeout": 0}, {"shutdown_timeout": True},
    {"shutdown_timeout": "1"}, {"shutdown_timeout": float("inf")},
    {"shutdown_timeout": float("nan")}, {"main": None},
])
def test_invalid_options(kwargs):
    assert run(**{"main": ready, **kwargs}) == 2


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
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        capture_output=True, text=True, check=False, timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_async_generator_cleanup_is_bounded(capsys):
    generators = []

    async def resource():
        try:
            yield
        finally:
            await asyncio.sleep(1)

    async def main(ctx):
        generator = resource()
        generators.append(generator)
        await anext(generator)
        ctx.ready()

    assert run(main, shutdown_timeout=0.02) == 1
    assert "async generator cleanup timed out" in capsys.readouterr().err


def test_task_suppressing_cancellation_cannot_hang_run(capsys):
    async def main(ctx):
        ctx.ready()
        ctx.stopping.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Finite even if the SDK regresses; the subprocess test checks an infinite wait.
            await asyncio.sleep(1)

    assert run(main, shutdown_timeout=0.02) == 1
    assert "task cleanup failed" in capsys.readouterr().err


def test_filesystem_denial_does_not_start_application(monkeypatch, capsys):
    def readonly(*_args, **_kwargs):
        raise OSError(30, "synthetic-secret-value")

    monkeypatch.setattr(Path, "mkdir", readonly)
    assert run(ready) == 2
    assert "synthetic-secret-value" not in capsys.readouterr().err
