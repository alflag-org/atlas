"""CLI/API contracts, daemon entry point, import isolation and startup failures."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from atlas.cli import main
from atlas.cluster import daemon
from atlas_core.cluster import ClusterClient, ClusterError, socket_path

from .test_cluster_process import Host


def test_socket_selection(monkeypatch, tmp_path):
    monkeypatch.delenv("ATLAS_CLUSTER_SOCKET", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert socket_path() == Path("/run/atlas/cluster/control.sock")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert socket_path() == tmp_path / "atlas/cluster/control.sock"
    monkeypatch.setenv("ATLAS_CLUSTER_SOCKET", str(tmp_path / "chosen"))
    assert socket_path() == tmp_path / "chosen"


def test_cli_and_public_models_against_real_entry_point(tmp_path, capsys):
    async def scenario():
        host = Host(tmp_path, "a")
        try:
            await host.start(entry=True)
            args = ["cluster", "--socket", str(host.runtime / "control.sock")]
            for method, extra in [
                ("status", []),
                ("init", ["--name", "lab"]),
                ("members", []),
                ("leave", []),
            ]:
                assert await asyncio.to_thread(main, [*args, method, *extra]) == 0
                output = capsys.readouterr()
                assert json.loads(output.out)
                assert output.err == ""
            async with ClusterClient.connect(host.runtime / "control.sock") as client:
                assert (await client.status()).membership == "left"
                assert (await client.members()).members[0].id == "a"
            assert await asyncio.to_thread(main, [*args, "join", "bad@host"]) == 2
            output = capsys.readouterr()
            assert (
                not output.out and json.loads(output.err)["error"] == "invalid_endpoint"
            )
        finally:
            await host.stop()
            host.output.close()
        assert await asyncio.to_thread(main, [*args, "status"]) == 2
        assert json.loads(capsys.readouterr().err)["error"] == "daemon_unavailable"

    asyncio.run(scenario())


def test_cli_removed_options_and_import_isolation(capsys):
    for args in (
        ["_registry"],
        ["join", "--via", "user@host"],
        ["join", "--abort"],
        ["leave", "--member", "a"],
        ["members", "--cached"],
    ):
        with pytest.raises(SystemExit) as error:
            main(["cluster", *args])
        assert error.value.code == 2
    capsys.readouterr()
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import atlas_core.cluster; import atlas_core.daemon; "
            "from atlas.cli import build_parser; build_parser(); "
            "assert not any(n.startswith(('cryptography', 'atlas.cluster.node')) for n in sys.modules)",
        ],
        check=True,
        env=dict(os.environ, PYTHONPATH="src"),
    )


def test_daemon_configuration_and_state_startup_errors(
    tmp_path, monkeypatch, application_logging, capsys
):
    assert daemon.main(["--config", str(tmp_path / "missing")]) == 2
    assert "invalid cluster configuration" in capsys.readouterr().err
    config = tmp_path / "cluster.yml"
    config.write_text(
        json.dumps(
            {
                "bind": "127.0.0.1:7947",
                "advertise": "localhost:7947",
                "credentials_file": str(tmp_path / "credentials"),
            }
        )
    )
    host = tmp_path / "host.yml"
    host.write_text("version: 1\nhost:\n  id: a\n")
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    path = state / "membership.json"
    path.write_text('{"schema":"atlas.membership/v1"}')
    path.chmod(0o600)
    monkeypatch.setenv("ATLAS_HOST_FILE", str(host))
    monkeypatch.setenv("STATE_DIRECTORY", str(state))
    monkeypatch.setenv("RUNTIME_DIRECTORY", str(tmp_path))
    assert daemon.main(["--config", str(config)]) == 1
    assert "legacy_state_requires_reconfiguration" in capsys.readouterr().err


def test_application_startup_failure_and_failed_required_loop(
    tmp_path, monkeypatch, application_logging
):
    from atlas.cluster.config import load_config
    from atlas.cluster.node import Node
    from atlas_core.daemon import DaemonContext
    from atlas_core.logging import get_logger

    from .test_cluster_process import free_port

    async def scenario():
        config_path = tmp_path / "cluster.yml"
        config_path.write_text(
            json.dumps(
                {
                    "bind": f"127.0.0.1:{free_port('127.0.0.1')}",
                    "advertise": "localhost:7947",
                    "credentials_file": str(tmp_path / "missing/credential"),
                }
            )
        )
        host = tmp_path / "host.yml"
        host.write_text("version: 1\nhost:\n  id: a\n")
        monkeypatch.setenv("ATLAS_HOST_FILE", str(host))
        config = load_config(config_path)
        ctx = DaemonContext("cluster", get_logger("cluster"), tmp_path, tmp_path)
        task = asyncio.create_task(daemon.application(ctx, config))
        try:
            while not ctx._ready:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
            with pytest.raises(ClusterError, match="configuration_or_storage_error"):
                await ClusterClient(tmp_path / "control.sock").request(
                    "init", {"name": "lab"}
                )
        finally:
            ctx.stopping.set()
            await task

        async def fail(self):
            raise RuntimeError("required loop failed")

        monkeypatch.setattr(Node, "probes", fail)
        ctx = DaemonContext("cluster", get_logger("cluster"), tmp_path, tmp_path)
        with pytest.RaisesGroup(RuntimeError, flatten_subgroups=True):
            await daemon.application(ctx, config)
        assert not (tmp_path / "control.sock").exists()

        (tmp_path / "control.sock").write_text("must not replace a regular file")
        ctx = DaemonContext("cluster", get_logger("cluster"), tmp_path, tmp_path)
        with pytest.RaisesGroup(ValueError):
            await daemon.application(ctx, config)
        assert not ctx._ready

    asyncio.run(scenario())
