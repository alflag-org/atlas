"""Real foreground processes verify signal, failure and bounded shutdown contracts."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def daemon_process(tmp_path, *, source=None):
    environment = dict(os.environ)
    for key in list(environment):
        if key.startswith("ATLAS_") or key in ("STATE_DIRECTORY", "RUNTIME_DIRECTORY", "NOTIFY_SOCKET", "XDG_RUNTIME_DIR"):
            environment.pop(key)
    environment["XDG_STATE_HOME"] = str(tmp_path / "state")
    environment["PYTHONPATH"] = str(ROOT / "src")
    argv = [sys.executable, "-m", "hoged"] if source is None else [sys.executable, "-c", source]
    with subprocess.Popen(argv, cwd=ROOT / "examples/daemon", env=environment,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
        try:
            yield process
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def wait_ready(process):
    with selectors.DefaultSelector() as selector:
        selector.register(process.stderr, selectors.EVENT_READ)
        assert selector.select(timeout=5), "daemon did not initialize"
        assert "starting" in process.stderr.readline()
        # readline may have prefetched the second line into its text buffer.
        assert "ready" in process.stderr.readline()


@pytest.mark.parametrize("number", [signal.SIGINT, signal.SIGTERM])
def test_hoged_runs_independently_in_foreground(tmp_path, number):
    with daemon_process(tmp_path) as process:
        wait_ready(process)
        assert process.poll() is None
        process.send_signal(number)
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0
        assert stdout == ""
        assert "stopped" in stderr


@pytest.mark.parametrize("mode", ["timeout", "resist", "child", "system-exit"])
def test_real_process_failure_and_cleanup_deadline(tmp_path, mode):
    source = f'''
import asyncio
from atlas_core.daemon import run
async def main(ctx):
    ctx.ready()
    if {mode!r} == "system-exit":
        raise SystemExit(7)
    if {mode!r} == "child":
        async def fail():
            raise RuntimeError("synthetic-secret-value")
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(fail())
            await ctx.wait_for_stop()
    ctx.stopping.set()
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        if {mode!r} == "resist":
            await asyncio.Event().wait()
        raise
raise SystemExit(run(main, shutdown_timeout=0.05))
'''
    with daemon_process(tmp_path, source=source) as process:
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == (7 if mode == "system-exit" else 1)
        assert not stdout
        assert "synthetic-secret-value" not in stderr
