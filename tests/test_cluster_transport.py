from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

import pytest

from atlas.cluster import network
from atlas.cluster.node import Node
from atlas_core.cluster import ClusterError, ClusterUnavailable


def ssh_script(tmp_path, monkeypatch, body):
    path = tmp_path / "ssh"
    path.write_text(f"#!{sys.executable}\nimport json, os, sys, time\n" + body)
    path.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))


def test_ssh_uses_existing_auth_and_a_fixed_remote_command(tmp_path, monkeypatch):
    payload = {
        "method": "join",
        "member": {"labels": {"literal": "$(touch bad); 'quoted'"}},
    }
    ssh_script(
        tmp_path,
        monkeypatch,
        "assert sys.argv[1:] == ['-T', '-oBatchMode=yes', '-oStrictHostKeyChecking=yes', '-oConnectTimeout=5', '--', 'ops@registry', 'atlas cluster _registry']\n"
        "request = json.load(sys.stdin)\n"
        "response = json.dumps({'schema': 'atlas.membership-rpc/v1', 'result': request}).encode()\n"
        "for byte in response:\n os.write(1, bytes([byte])); time.sleep(.0001)\n",
    )
    assert asyncio.run(network.request("ops@registry", payload, timeout=5)) == payload
    assert not (tmp_path / "bad").exists()


@pytest.mark.parametrize(
    "address", ["-oProxyCommand=x", "ops@host;id", "host\nother", "$(id)", "", None]
)
def test_ssh_destinations_cannot_supply_options_or_shell_code(address):
    with pytest.raises(ClusterError, match="invalid_address"):
        asyncio.run(network.request(address, {}, timeout=1))


@pytest.mark.parametrize(
    "body,code",
    [
        ("print('not json')", "invalid_response"),
        ("print('{}')", "invalid_response"),
        ("print('null')", "invalid_response"),
        ("print(json.dumps({'schema':'wrong','result':{}}))", "invalid_response"),
        (
            "print(json.dumps({'schema':'atlas.membership-rpc/v1','error':{'secret':'value'}}))",
            "invalid_response",
        ),
        (
            "print(json.dumps({'schema':'atlas.membership-rpc/v1','error':'member_conflict'}))",
            "member_conflict",
        ),
        (
            "sys.stderr.write('credential detail'); sys.exit(255)",
            "registry_unavailable",
        ),
        (
            "sys.stdout.write('x' * (1024 * 1024 + 1)); sys.stdout.flush(); time.sleep(5)",
            "invalid_response",
        ),
    ],
)
def test_ssh_protocol_failures_are_bounded_and_sanitized(
    tmp_path, monkeypatch, body, code
):
    ssh_script(tmp_path, monkeypatch, "sys.stdin.read()\n" + body + "\n")
    with pytest.raises(ClusterError, match=f"^{code}$"):
        asyncio.run(network.request("registry", {}, timeout=3))


@pytest.mark.parametrize("cancel", [False, True])
def test_timeout_and_cancellation_terminate_ssh(tmp_path, monkeypatch, cancel):
    pidfile = tmp_path / "pid"
    ssh_script(
        tmp_path,
        monkeypatch,
        f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\nsys.stdin.read()\ntime.sleep(60)\n",
    )

    async def scenario():
        task = asyncio.create_task(
            network.request("registry", {}, timeout=0.5 if not cancel else 10)
        )
        async with asyncio.timeout(3):
            while not pidfile.exists():
                await asyncio.sleep(0.01)
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else ClusterUnavailable):
            await task
        with pytest.raises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)

    asyncio.run(scenario())


def test_ssh_exit_race_during_cleanup(tmp_path, monkeypatch):
    ssh_script(tmp_path, monkeypatch, "sys.stdin.read()\ntime.sleep(60)\n")
    kill = os.killpg

    def disappeared(pid, sig):
        kill(pid, sig)
        raise ProcessLookupError

    monkeypatch.setattr(os, "killpg", disappeared)
    with pytest.raises(ClusterUnavailable):
        asyncio.run(network.request("registry", {}, timeout=0.2))


def test_missing_ssh_and_oversize_request_fail_before_registration(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(ClusterUnavailable):
        asyncio.run(network.request("registry", {}, timeout=1))
    with pytest.raises(ClusterError, match="membership_too_large"):
        asyncio.run(network.request("registry", {"data": "x" * 1024 * 1024}, timeout=1))


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_deadlines_are_rejected(timeout):
    with pytest.raises(ValueError):
        Node(timeout=timeout)
    with pytest.raises(ValueError):
        asyncio.run(network.request("registry", {}, timeout=timeout))


@pytest.mark.parametrize("cancel", [False, True])
def test_cleanup_kills_ssh_descendants_after_parent_exit(tmp_path, monkeypatch, cancel):
    child_pid = tmp_path / "child.pid"
    ssh_script(
        tmp_path,
        monkeypatch,
        "sys.stdin.read()\n"
        "if os.fork() == 0:\n"
        f" open({str(child_pid)!r}, 'w').write(str(os.getpid()))\n"
        " time.sleep(60)\n"
        "os._exit(0)\n",
    )

    async def scenario():
        task = asyncio.create_task(
            network.request("registry", {}, timeout=0.5 if not cancel else 10)
        )
        async with asyncio.timeout(3):
            while not child_pid.exists():
                await asyncio.sleep(0.01)
        if cancel:
            await asyncio.sleep(0.05)
            task.cancel()
        try:
            with pytest.raises(
                asyncio.CancelledError if cancel else ClusterUnavailable
            ):
                await task
            # An orphan may remain a zombie until PID 1 reaps it; it must not run.
            status = Path(f"/proc/{child_pid.read_text()}/stat")
            async with asyncio.timeout(3):
                while status.exists():
                    if status.read_text().split(")", 1)[1].split()[0] == "Z":
                        break
                    await asyncio.sleep(0.01)
        finally:
            try:
                os.kill(int(child_pid.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass

    asyncio.run(scenario())
