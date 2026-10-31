from __future__ import annotations

import asyncio
import os
import socket
from pathlib import Path

import pytest

from atlas.cluster.node import Node
from atlas_core.cluster import ClusterClient, ClusterUnavailable
from atlas_core.ipc import IPCError, serve


def address():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return f"tls://127.0.0.1:{sock.getsockname()[1]}"


def node(root: Path, name: str):
    directory = root / name
    directory.mkdir(mode=0o700)
    return Node(directory, address(), name)


def test_three_nodes_join_revoke_restart_and_partition(tmp_path):
    async def scenario():
        authority, worker, other = [
            node(tmp_path, name) for name in ("authority", "worker", "other")
        ]
        try:
            assert authority.listener.server is None
            assert authority.status()["cluster_id"] is None
            await authority.initialize({"name": "lab"})
            invite = authority.invitation({"labels": {"group": "app"}})
            await worker.join({"invite": invite})
            assert worker.status()["cluster_id"] == authority.status()["cluster_id"]
            assert worker.node_id != authority.node_id
            await other.join({"invite": authority.invitation({})})
            members = await worker.authority_call("members", {})
            assert len(members["members"]) == 3
            assert members["revision"] == 3
            with pytest.raises(IPCError, match="forbidden"):
                await worker.authority_call("invite", {})
            await authority.authority_call("remove", {"node_id": worker.node_id})
            with pytest.raises(IPCError, match="forbidden"):
                await worker.authority_call("members", {})
            directory, old_id = authority.directory, authority.node_id
            await authority.close()
            with pytest.raises(IPCError, match="unavailable"):
                await other.authority_call("members", {})
            authority = Node(directory, None, "authority renamed")
            await authority.start()
            assert authority.node_id == old_id
            assert (await other.authority_call("members", {}))["revision"] == 4
            await other.authority_call("leave", {})
            with pytest.raises(IPCError, match="forbidden"):
                await other.authority_call("members", {})
        finally:
            await asyncio.gather(*(item.close() for item in (authority, worker, other)))

    asyncio.run(scenario())


def test_local_client_runs_without_network_or_initialization(tmp_path):
    async def scenario():
        local = node(tmp_path, "local")
        path = tmp_path / "local" / "control.sock"
        try:
            async with serve(
                path,
                api_version="atlas.cluster/v1",
                handler=local.local,
                authorize=lambda peer, method: peer.uid == os.getuid(),
            ):
                async with ClusterClient.connect(path) as client:
                    status = await client.status()
                    assert status.node_id == local.node_id
                    assert status.cluster_id is None
                    with pytest.raises(IPCError, match="not_joined"):
                        await client.members()
                    await client.request("init", {"name": "lab"})
                    members = await client.members()
                    assert members[0].node_id == local.node_id
                    assert members[0].permissions == ("read", "execute", "admin")
            with pytest.raises(ClusterUnavailable):
                await ClusterClient(path).status()
        finally:
            await local.close()

    asyncio.run(scenario())


def test_invitation_is_single_use_and_identity_is_not_display_name(tmp_path):
    async def scenario():
        authority, worker, other = [node(tmp_path, name) for name in ("a", "b", "c")]
        try:
            await authority.initialize({"name": "lab"})
            invite = authority.invitation({})
            worker.display_name = authority.display_name
            await worker.join({"invite": invite})
            assert worker.node_id != authority.node_id
            with pytest.raises(IPCError, match="invalid_invitation"):
                await other.join({"invite": invite})
            assert authority.store.get("revision") == 2
            assert other.status()["cluster_id"] is None
            with pytest.raises(IPCError, match="cannot_remove_authority"):
                await authority.authority_call("leave", {})
        finally:
            await asyncio.gather(*(item.close() for item in (authority, worker, other)))

    asyncio.run(scenario())


def test_admission_recovery_pins_identity_without_reusing_invitation(
    tmp_path, monkeypatch
):
    from atlas.cluster import network
    from atlas_core.ipc import Unavailable

    async def scenario():
        authority, worker = node(tmp_path, "authority"), node(tmp_path, "worker")
        try:
            await authority.initialize({"name": "lab"})
            invite = authority.invitation({})
            call = network.call

            async def lose_reply(*args, **kwargs):
                await call(*args, **kwargs)
                raise Unavailable()

            monkeypatch.setattr(network, "call", lose_reply)
            with pytest.raises(Unavailable):
                await worker.join({"invite": invite})
            original_key = worker.store.get("key")
            with pytest.raises(IPCError, match="join_outcome_unknown"):
                await worker.join({"invite": invite})
            with pytest.raises(IPCError, match="join_outcome_unknown"):
                await worker.initialize({"name": "other"})
            with pytest.raises(IPCError, match="admission_not_found"):
                await worker.join(
                    {"invite": invite | {"secret": "changed"}, "reconcile": True}
                )
            monkeypatch.setattr(network, "call", call)
            directory, endpoint = worker.directory, worker.address
            await worker.close()
            worker = Node(directory, endpoint, "worker")
            await worker.join({"invite": invite, "reconcile": True})
            assert worker.store.get("key") == original_key
            assert len(authority.store.members()) == 2
            assert authority.store.get("revision") == 2
        finally:
            await asyncio.gather(authority.close(), worker.close())

    asyncio.run(scenario())


def test_invalid_membership_requests_and_pinned_trust(tmp_path):
    import time
    from uuid import uuid4

    from atlas.cluster import identity, network

    async def scenario():
        authority, worker = node(tmp_path, "authority"), node(tmp_path, "worker")
        unconfigured = Node(tmp_path, None, "no listener")
        try:
            await unconfigured.start()
            with pytest.raises(IPCError, match="endpoint_required"):
                await unconfigured.initialize({"name": "x"})
            for method in ("join", "admission"):
                with pytest.raises(IPCError, match="not_authority"):
                    await worker.remote(method, {}, None)
            with pytest.raises(IPCError, match="forbidden"):
                await worker.remote("members", {}, None)
            await authority.initialize({"name": "lab"})
            await authority.start()
            with pytest.raises(IPCError, match="already_joined"):
                await authority.initialize({"name": "again"})
            for options, code in (
                ({"ttl_seconds": 0}, "invalid_expiry"),
                ({"permissions": ["root"]}, "invalid_permissions"),
                ({"labels": []}, "invalid_labels"),
                ({"labels": {"k": 1}}, "invalid_request"),
            ):
                with pytest.raises(IPCError, match=code):
                    authority.invitation(options)
            invite = authority.invitation({})
            for bad in ({}, invite | {"expires": time.time() - 1}):
                with pytest.raises(IPCError, match="invalid_invitation"):
                    await worker.join({"invite": bad})
            for method in ("unknown",):
                with pytest.raises(IPCError, match="unknown_method"):
                    await authority.local(method, {}, None)
                with pytest.raises(IPCError, match="unknown_method"):
                    await authority.authority_call(method, {})
            with pytest.raises(IPCError, match="unknown_member"):
                await authority.authority_call("remove", {"node_id": str(uuid4())})
            with pytest.raises(IPCError, match="invalid_id"):
                await authority.authority_call("remove", {"node_id": "bad"})
            ca = identity.issue(identity.new_key(), str(uuid4()))
            with pytest.raises(IPCError, match="unavailable"):
                await network.call(
                    authority.address,
                    authority.node_id,
                    identity.tls_context(tmp_path, ca),
                    "join",
                    {},
                )
            with pytest.raises(IPCError, match="forbidden"):
                await network.call(
                    authority.address,
                    authority.node_id,
                    identity.tls_context(tmp_path, invite["ca"]),
                    "members",
                    {},
                )
            key = identity.new_key()
            request = {
                "invite": invite,
                "node_id": authority.node_id,
                "endpoint": worker.address,
                "display_name": "same",
                "csr": identity.request_identity(key, worker.node_id),
            }
            with pytest.raises(IPCError, match="identity_exists"):
                authority.admit(request)
            with pytest.raises(IPCError, match="admission_not_found"):
                authority.admission(request)
            with authority.store.db:
                authority.store.db.execute("UPDATE invitations SET expires=0")
            with pytest.raises(IPCError, match="invalid_invitation"):
                await worker.join({"invite": invite})
            # A rejected join can be followed by a fresh invitation.
            await worker.local(
                "join",
                {
                    "invite": authority.invitation(
                        {"permissions": ["read", "execute", "admin"]}
                    )
                },
                None,
            )
            assert (await worker.authority_call("invite", {}))[
                "schema"
            ] == "atlas.cluster-invite/v1"
            await worker.local("leave", {}, None)
        finally:
            await asyncio.gather(
                authority.close(), worker.close(), unconfigured.close()
            )

    asyncio.run(scenario())


def test_join_internal_failure_keeps_pending_identity(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from atlas.cluster import network

    async def scenario():
        authority, worker = node(tmp_path, "authority"), node(tmp_path, "worker")
        try:
            await authority.initialize({"name": "lab"})
            invite = authority.invitation({})
            monkeypatch.setattr(
                network, "call", AsyncMock(side_effect=IPCError("internal_error"))
            )
            with pytest.raises(IPCError, match="internal_error"):
                await worker.join({"invite": invite})
            assert worker.store.get("join_pending")
            assert worker.store.get("key")
        finally:
            await asyncio.gather(authority.close(), worker.close())

    asyncio.run(scenario())


def test_execution_permission_does_not_require_membership_read_access(
    tmp_path, monkeypatch
):
    from atlas.cluster.workflow import command_identity

    from .support import configure_environment, write_native_command

    root = tmp_path / "program"
    write_native_command(root, "sample")
    configure_environment(
        monkeypatch,
        tmp_path,
        f"programs:\n  app:\n    root: {root}\n    runtime:\n      type: native\n",
    )

    async def scenario():
        authority, worker, reader = [
            node(tmp_path, name) for name in ("authority", "worker", "reader")
        ]
        try:
            await authority.initialize({"name": "lab"})
            await worker.join(
                {"invite": authority.invitation({"permissions": ["execute"]})}
            )
            await reader.join(
                {"invite": authority.invitation({"permissions": ["read"]})}
            )
            inspected = await authority.workflows.peer(
                worker.node_id, "command.inspect", {"command": "sample"}
            )
            assert inspected == command_identity("sample")
            planned = await worker.authority_call(
                "plan",
                {
                    "workflow": {
                        "schema": "atlas.workflow/v1",
                        "name": "check",
                        "steps": [
                            {
                                "id": "check",
                                "targets": {"nodes": [worker.node_id]},
                                "command": "sample",
                                "mutates": False,
                            },
                        ],
                    }
                },
            )
            operation = await worker.authority_call(
                "apply", planned | {"confirm": planned["digest"]}
            )
            async with asyncio.timeout(5):
                while authority.workflows.operation(operation["operation_id"])[
                    "state"
                ] in {"prepared", "running"}:
                    await asyncio.sleep(0.01)
            assert (
                authority.workflows.operation(operation["operation_id"])["state"]
                == "succeeded"
            )
            with pytest.raises(IPCError, match="forbidden"):
                await worker.authority_call("members", {})
            assert (await reader.local("leave", {}, None))["removed"] == reader.node_id
            assert (await worker.local("leave", {}, None))["removed"] == worker.node_id
        finally:
            await asyncio.gather(authority.close(), worker.close(), reader.close())

    asyncio.run(scenario())
