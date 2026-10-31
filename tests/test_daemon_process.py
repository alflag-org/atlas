"""Real foreground processes verify signal, failure and bounded shutdown contracts."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from textwrap import dedent, indent

import pytest

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def daemon_process(tmp_path, *, source=None):
    environment = dict(os.environ)
    daemon_keys = {"STATE_DIRECTORY", "RUNTIME_DIRECTORY", "NOTIFY_SOCKET", "XDG_RUNTIME_DIR"}
    environment = {
        key: value for key, value in environment.items()
        if not key.startswith("ATLAS_") and key not in daemon_keys
    }
    environment["XDG_STATE_HOME"] = str(tmp_path / "state")
    environment["PYTHONPATH"] = str(ROOT / "src")
    argv = [sys.executable, "-m", "sampled"] if source is None else [sys.executable, "-c", source]
    with subprocess.Popen(
        argv,
        cwd=ROOT / "examples/daemon",
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        try:
            yield process
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def wait_ready(process):
    with selectors.DefaultSelector() as selector:
        selector.register(process.stderr, selectors.EVENT_READ)
        deadline = time.monotonic() + 5
        output = b""
        while b"sampled: INFO: ready\n" not in output:
            remaining = max(0, deadline - time.monotonic())
            assert selector.select(timeout=remaining), "daemon did not initialize"
            chunk = os.read(process.stderr.fileno(), 4096)
            assert chunk, "daemon exited before readiness"
            output += chunk
        assert b"starting" in output


@pytest.mark.parametrize("number", [signal.SIGINT, signal.SIGTERM])
def test_sampled_runs_independently_in_foreground(tmp_path, number):
    with daemon_process(tmp_path) as process:
        wait_ready(process)
        assert process.poll() is None
        process.send_signal(number)
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0
        assert stdout == ""
        assert "stopped" in stderr


@pytest.mark.parametrize(("body", "expected_status", "diagnostic"), [
    pytest.param("raise SystemExit(7)", 7, "entered", id="system-exit"),
    pytest.param("""
        ctx.stopping.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.Event().wait()
    """, 1, "task cleanup failed", id="uncooperative-shutdown"),
])
def test_process_terminates_even_when_application_does_not_return(
    tmp_path, body, expected_status, diagnostic,
):
    # These cases need a process deadline: a regression must not hang the test runner.
    source = (
        "import asyncio\n"
        "from atlas_core.daemon import run\n"
        "async def main(ctx):\n"
        "    ctx.ready()\n"
        "    ctx.logger.info('entered')\n"
        + indent(dedent(body).strip(), "    ")
        + "\nraise SystemExit(run(main, shutdown_timeout=0.05))\n"
    )
    with daemon_process(tmp_path, source=source) as process:
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == expected_status, stderr
        assert diagnostic in stderr
        assert not stdout
