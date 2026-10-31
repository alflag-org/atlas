from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from atlas_core.cluster import ClusterClient, ClusterError, ClusterUnavailable

from .test_cluster_membership import address


def test_worker_and_authority_crash_never_replay_unknown_mutations():
    """Three OS processes, separate state/configuration, real TLS and Executor runs."""
    with tempfile.TemporaryDirectory(prefix="atlas-cluster-process-") as temporary:
        root = Path(temporary)
        processes = {}
        environments = {}
        clients = {}
        streams = []
        for name in ("authority", "one", "two"):
            directory = root / name
            directory.mkdir(mode=0o700)
            for child in ("state", "runtime", "etc", "program", "program/bin"):
                (directory / child).mkdir(mode=0o700)
            script = directory / "program/bin/mutate"
            script.write_text(
                f"#!{sys.executable}\n"
                "import json, os, pathlib, time\n"
                f"root = pathlib.Path({str(directory)!r})\n"
                "with (root / 'launches').open('a') as stream: stream.write('x')\n"
                "(root / 'started').write_text(str(os.getpid()))\n"
                "while not (root / 'release').exists(): time.sleep(0.01)\n"
                "(root / 'finished').touch()\n"
            )
            script.chmod(0o700)
            (directory / "etc/config.yml").write_text(
                f"programs:\n  app:\n    root: {directory / 'program'}\n    runtime:\n      type: native\n"
            )
            (directory / "etc/host.yml").write_text(
                f"version: 1\nhost:\n  id: {name}\n  role: test\n  site: test\n"
            )
            environments[name] = os.environ | {
                "STATE_DIRECTORY": str(directory / "state"),
                "RUNTIME_DIRECTORY": str(directory / "runtime"),
                "ATLAS_ETC_DIR": str(directory / "etc"),
                "ATLAS_VAR_DIR": str(directory / "var"),
                "ATLAS_HOME": str(directory / "home"),
            }
            environments[name].pop("NOTIFY_SOCKET", None)
            clients[name] = ClusterClient(directory / "runtime/control.sock", timeout=5)

        def start(name):
            stream = (root / name / "stderr.log").open("ab")
            streams.append(stream)
            processes[name] = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "atlas.cluster.daemon",
                    "--endpoint",
                    address(),
                    "--display-name",
                    name,
                ],
                env=environments[name],
                stdout=subprocess.DEVNULL,
                stderr=stream,
            )

        async def ready(name):
            async with asyncio.timeout(10):
                while True:
                    assert processes[name].poll() is None, (
                        root / name / "stderr.log"
                    ).read_text()
                    try:
                        return await clients[name].status()
                    except ClusterUnavailable:
                        await asyncio.sleep(0.02)

        async def wait_files(filename):
            async with asyncio.timeout(10):
                while not all(
                    (root / name / filename).exists() for name in ("one", "two")
                ):
                    await asyncio.sleep(0.02)

        async def scenario():
            for name in clients:
                start(name)
                await ready(name)
            await clients["authority"].request("init", {"name": "lab"})
            for name in ("one", "two"):
                invitation = await clients["authority"].request(
                    "invite", {"labels": {"group": "app"}}
                )
                await clients[name].request("join", {"invite": invitation})
            workflow = {
                "schema": "atlas.workflow/v1",
                "name": "mutate",
                "steps": [
                    {
                        "id": "change",
                        "targets": {"group": "app"},
                        "command": "mutate",
                        "mutates": True,
                        "max_parallel": 2,
                    },
                ],
            }
            planned = await clients["one"].request("plan", {"workflow": workflow})
            applied = await clients["two"].request(
                "apply", planned | {"confirm": planned["digest"]}
            )
            await wait_files("started")
            for name in ("one", "authority"):
                processes[name].kill()
                assert processes[name].wait(timeout=5) == -signal.SIGKILL
            with pytest.raises(ClusterUnavailable):
                await clients["two"].members()
            for name in ("authority", "one"):
                start(name)
                await ready(name)
            repeated = await clients["two"].request(
                "apply", planned | {"confirm": planned["digest"]}
            )
            assert repeated["state"] == "unknown"
            second = await clients["two"].request("plan", {"workflow": workflow})
            with pytest.raises(ClusterError, match="operation_in_progress"):
                await clients["two"].request(
                    "apply", second | {"confirm": second["digest"]}
                )
            for name in ("one", "two"):
                (root / name / "release").touch()
            await wait_files("finished")
            reconciled = await clients["authority"].request(
                "operation.reconcile", {"operation_id": applied["operation_id"]}
            )
            assert reconciled["state"] == "unknown"
            async with asyncio.timeout(10):
                while not all(
                    (root / name / "var/logs/runs.jsonl").exists()
                    for name in ("one", "two")
                ):
                    await asyncio.sleep(0.02)
            for name in ("one", "two"):
                assert (root / name / "launches").read_text() == "x"
                record = json.loads((root / name / "var/logs/runs.jsonl").read_text())
                assert record["operation_id"] == applied["operation_id"]
                assert record["parent_run_id"]

        try:
            asyncio.run(scenario())
        finally:
            for name in ("one", "two"):
                (root / name / "release").touch()
            for process in processes.values():
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            for stream in streams:
                stream.close()
