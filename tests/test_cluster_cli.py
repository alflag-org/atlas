from __future__ import annotations

import io
import json

import pytest

from atlas.cli import main
from atlas.cluster import network
from atlas.cluster.node import Node

from .test_cluster_membership import metadata


def test_operator_commands_and_json_contract(tmp_path, monkeypatch, capsys):
    registry = tmp_path / "registry"
    worker = tmp_path / "worker"
    host = tmp_path / "host.yml"
    host.write_text("version: 1\nhost:\n  id: registry\n  role: app\n  site: home\n")
    monkeypatch.setenv("ATLAS_HOST_FILE", str(host))

    def command(directory, *args):
        code = main(["cluster", "--directory", str(directory), *args])
        captured = capsys.readouterr()
        return code, json.loads(captured.out or captured.err), captured

    code, result, output = command(
        registry,
        "init",
        "--name",
        "lab",
        "--address",
        "registry",
        "--label",
        "env=test",
    )
    assert code == 0 and not output.err
    assert result["members"][0]["labels"] == {"env": "test"}
    host.write_text("version: 1\nhost:\n  id: worker\n")

    async def request(address, payload, *, timeout):
        assert address == "ops@registry"
        return Node(registry).registry_request(payload)

    monkeypatch.setattr(network, "request", request)
    assert (
        command(worker, "join", "--via", "ops@registry", "--address", "worker")[0] == 0
    )
    assert command(worker, "status")[1]["joined"]
    assert command(worker, "members", "--cached")[1]["stale"]
    assert command(worker, "leave")[1] == {"removed": "worker"}
    code, result, output = command(worker, "members")
    assert code == 2 and not output.out
    assert result == {"schema": "atlas.cluster-error/v1", "error": "not_joined"}
    code, result, output = command(
        worker, "join", "--via", "registry", "--label", "missing-equals"
    )
    assert code == 2 and result["error"] == "invalid_configuration" and not output.out
    code, result, _ = command(registry, "leave", "--member", "worker")
    assert code == 0 and result == {"removed": "worker"}


def test_ssh_registry_entry_point_is_membership_only(tmp_path, monkeypatch, capsys):
    node = Node(tmp_path / "registry")
    node.initialize("lab", metadata("registry"))
    for request, error in [
        (json.dumps({"method": "members"}), None),
        (json.dumps({"method": "execute", "command": "anything"}), "unknown_method"),
        ('["members"]', "invalid_request"),
        ("x" * (1024 * 1024 + 1), "invalid_request"),
        ("null", "invalid_request"),
    ]:
        monkeypatch.setattr("sys.stdin", io.StringIO(request))
        assert (
            main(["cluster", "--directory", str(node.store.directory), "_registry"])
            == 0
        )
        captured = capsys.readouterr()
        assert not captured.err
        reply = json.loads(captured.out)
        assert reply["schema"] == "atlas.membership-rpc/v1"
        if error:
            assert reply["error"] == error
        else:
            assert reply["result"]["members"] == [metadata("registry")]


def test_cli_does_not_offer_job_execution_or_daemon_startup(capsys):
    with pytest.raises(SystemExit) as result:
        main(["cluster", "--help"])
    assert result.value.code == 0
    help_text = capsys.readouterr().out
    assert "{init,join,leave,members,status}" in help_text
    for removed in ("plan", "apply", "operation", "invite"):
        with pytest.raises(SystemExit) as error:
            main(["cluster", removed])
        assert error.value.code == 2


def test_operator_can_correct_an_unreachable_pending_join(
    tmp_path, monkeypatch, capsys
):
    from atlas_core.cluster import ClusterUnavailable

    directory = tmp_path / "worker"
    host = tmp_path / "host.yml"
    host.write_text("version: 1\nhost:\n  id: worker\n")
    monkeypatch.setenv("ATLAS_HOST_FILE", str(host))
    registry = Node(tmp_path / "registry")
    registry.initialize("lab", metadata("registry"))

    async def request(address, payload, *, timeout):
        if address == "ops@wrong":
            raise ClusterUnavailable("registry_unavailable")
        return registry.registry_request(payload)

    monkeypatch.setattr(network, "request", request)
    args = ["cluster", "--directory", str(directory), "join"]
    assert main([*args, "--via", "ops@wrong"]) == 2
    capsys.readouterr()
    assert main([*args, "--via", "ops@correct"]) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "already_joined"
    assert main([*args, "--abort"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["registry"] == "ops@wrong" and result["warning"]
    assert main([*args, "--via", "ops@correct"]) == 0
    assert json.loads(capsys.readouterr().out)["name"] == "lab"
    assert main([*args, "--abort"]) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "not_pending_join"
