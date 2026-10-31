from __future__ import annotations

import asyncio
import json
import os
import signal
from unittest.mock import AsyncMock

import pytest

from atlas.cli import main
from atlas.cluster.cli import private_json, write_private
from atlas.cluster.daemon import main as daemon_main
from atlas_core.cluster import ClusterError, ClusterUnavailable
from atlas_core.ipc import Client


def test_operator_inputs_and_private_output(tmp_path, monkeypatch, capsys):
    request = AsyncMock(return_value={"schema": "atlas.cluster-invite/v1"})
    monkeypatch.setattr("atlas_core.cluster.ClusterClient.request", request)
    invitation = tmp_path / "invite.json"
    assert (
        main(
            [
                "cluster",
                "--socket",
                "/tmp/example.sock",
                "invite",
                "create",
                "--output",
                str(invitation),
                "--ttl",
                "30s",
                "--label",
                "group=app",
            ]
        )
        == 0
    )
    assert invitation.stat().st_mode & 0o777 == 0o600
    assert request.call_args.args[1]["ttl_seconds"] == 30
    assert request.call_args.args[1]["labels"] == {"group": "app"}
    assert (
        main(["cluster", "join", "--invite-file", str(invitation), "--reconcile"]) == 0
    )
    assert request.call_args.args[1]["reconcile"] is True
    assert main(["cluster", "init", "--name", "lab"]) == 0
    assert request.call_args.args == ("init", {"name": "lab"})
    assert main(["cluster", "remove", "node-id"]) == 0
    assert request.call_args.args == ("remove", {"node_id": "node-id"})
    workflow = tmp_path / "workflow.yml"
    workflow.write_text("name: deploy\nschema: atlas.workflow/v1\nsteps: []\n")
    plan_path = tmp_path / "plan.json"
    request.return_value = {"plan": {}, "digest": "digest"}
    assert (
        main(
            [
                "cluster",
                "plan",
                "deploy",
                "--workflow",
                str(workflow),
                "--out",
                str(plan_path),
            ]
        )
        == 0
    )
    assert main(["cluster", "apply", str(plan_path), "--confirm", "digest"]) == 0
    assert request.call_args.args == (
        "apply",
        {"plan": {}, "digest": "digest", "confirm": "digest"},
    )
    for action in ("inspect", "reconcile"):
        assert main(["cluster", "operation", action, "op-id"]) == 0
        assert request.call_args.args == (
            f"operation.{action}",
            {"operation_id": "op-id"},
        )
    assert (
        main(
            [
                "cluster",
                "plan",
                "other",
                "--workflow",
                str(workflow),
                "--out",
                str(plan_path),
            ]
        )
        == 2
    )
    assert "does not match" in capsys.readouterr().err
    for error in (ClusterError("forbidden"), ClusterUnavailable("unavailable")):
        request.side_effect = error
        assert main(["cluster", "status"]) == 2
        assert json.loads(capsys.readouterr().err)["error"] == error.code
    with pytest.raises(FileExistsError):
        write_private(plan_path, {})
    assert private_json(plan_path)["digest"] == "digest"
    plan_path.chmod(0o644)
    with pytest.raises(ValueError, match="owner-only"):
        private_json(plan_path)
    plan_path.chmod(0o600)
    plan_path.write_text("[]")
    with pytest.raises(TypeError):
        private_json(plan_path)


def test_atlasd_serves_before_initialization_and_stops_cleanly(tmp_path, monkeypatch):
    state, runtime = tmp_path / "state", tmp_path / "runtime"
    state.mkdir(mode=0o700)
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("STATE_DIRECTORY", str(state))
    monkeypatch.setenv("RUNTIME_DIRECTORY", str(runtime))
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    from atlas.cluster import daemon
    from atlas_core.daemon import run

    def exercise(application, **options):
        async def wrapped(ctx):
            task = asyncio.create_task(application(ctx))
            socket = runtime / "control.sock"
            async with asyncio.timeout(5):
                while not socket.exists():
                    await asyncio.sleep(0.01)
                status = await Client(socket, api_version="atlas.cluster/v1").request(
                    "status"
                )
            assert status["cluster_id"] is None
            os.kill(os.getpid(), signal.SIGTERM)
            await task

        return run(wrapped, **options)

    monkeypatch.setattr(daemon, "run", exercise)
    assert daemon_main(["--display-name", "test-node"]) == 0
    assert not (runtime / "control.sock").exists()
    assert (state / "cluster.sqlite3").is_file()
