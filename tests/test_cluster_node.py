"""Control and protocol regressions with precise fault injection at I/O boundaries."""

import asyncio
from dataclasses import asdict, replace
from uuid import uuid4

import pytest

from atlas.cluster import node as node_module
from atlas.cluster.config import Config
from atlas.cluster.membership import Membership
from atlas.cluster.network import DatagramInbox
from atlas.cluster.node import Node, PendingProbe, Timers
from atlas.cluster.store import Credential, State, Store
from atlas_core.cluster import ClusterError, MemberState

from .test_cluster_membership import member


def make_node(tmp_path, *, initialized=True):
    own = member()
    config = Config(own.endpoint, own.endpoint, tmp_path / "credential")
    membership = Membership(Store(tmp_path), State(own.id), own, 0.1)
    node = Node(config, membership, Timers(0.02, 0.01, 0.1, 0.01, 0.01, 0.05, 0.1))
    if initialized:
        node.initialize("lab")
    return node


def test_control_errors_and_credential_mismatch(tmp_path):
    async def scenario():
        node = make_node(tmp_path, initialized=False)
        assert (await node.control("status", {}))["membership"] == "uninitialized"
        assert not await node.synchronize(member().endpoint)
        await node.send(member().endpoint, "ping", {})
        for method, params, error in [
            ("members", {}, "not_joined"),
            ("leave", {}, "not_joined"),
            ("join", {"seeds": []}, "invalid_seeds"),
            ("join", {"seeds": "a"}, "invalid_seeds"),
            ("bad", {}, "invalid_request"),
            ("status", {"x": 1}, "invalid_request"),
        ]:
            with pytest.raises(ClusterError, match=error):
                await node.control(method, params)
        with pytest.raises(ClusterError, match="not_joined"):
            await node.remote("fetch", {})
        node.initialize("lab")
        with pytest.raises(ClusterError, match="already_initialized"):
            node.initialize("lab")
        for raw, error in [
            ({}, "invalid_snapshot"),
            ({"name": "other", "members": []}, "cluster_name_conflict"),
        ]:
            with pytest.raises(ClusterError, match=error):
                node.receive_snapshot(raw)
        with pytest.raises(ClusterError, match="invalid_request"):
            await node.remote("bad", {})
        credential = Credential.read(node.config.credentials_file)
        node.membership.state = replace(node.state, cluster_id=str(uuid4()))
        with pytest.raises(ClusterError, match="cluster_mismatch"):
            node.credentials()
        node.membership.state = replace(
            node.state, cluster_id=credential.cluster_id, credential_fingerprint="wrong"
        )
        with pytest.raises(ClusterError, match="credential_mismatch"):
            node.credentials()
        node.membership.failed = True
        for call in (node.remote("fetch", {}), node.control("status", {})):
            with pytest.raises(ClusterError, match="persistence_failed"):
                await call

    asyncio.run(scenario())


def test_join_failure_before_and_after_persistence_and_deadline(tmp_path, monkeypatch):
    async def scenario():
        node = make_node(tmp_path)
        saved = node.membership.store.path.read_bytes()
        calls = []

        async def exchange(peer, cipher, method, data, **kwargs):
            calls.append(method)
            if method == "fetch":
                return {"name": "lab", "members": [asdict(member("b"))]}
            raise ClusterError("peer_unavailable")

        monkeypatch.setattr(node_module, "exchange", exchange)
        with pytest.raises(ClusterError, match="peer_unavailable"):
            await node.join((member("b").endpoint,))
        assert calls == ["fetch", "sync"]
        assert node.state.membership == "joined" and "b" in node.state.members
        assert node.membership.store.path.read_bytes() != saved

        async def hang(*args, **kwargs):
            await asyncio.Event().wait()

        monkeypatch.setattr(node_module, "exchange", hang)
        with pytest.raises(ClusterError, match="join_timeout"):
            await node.join((member("b").endpoint,))
        result = await node.leave()
        assert result["membership"] == "left" and not result["propagated_to_peer"]
        current = node.state.members["a"]
        await node.leave()
        assert node.state.members["a"] == current

        def failed_write(state):
            raise OSError("sensitive storage message")

        monkeypatch.setattr(node_module, "exchange", exchange)
        monkeypatch.setattr(node.membership.store, "write", failed_write)
        with pytest.raises(ClusterError, match="persistence_failed"):
            await node.join((member("b").endpoint,))
        assert node.state.membership == "left"

    asyncio.run(scenario())


def test_sync_storage_failure_and_successful_leave(tmp_path, monkeypatch):
    async def scenario():
        node = make_node(tmp_path)
        node.membership.apply((member("b"),))

        async def remote(*args, **kwargs):
            return {"name": "lab", "members": [asdict(member("b", incarnation=2))]}

        monkeypatch.setattr(node_module, "exchange", remote)
        assert (await node.leave())["propagated_to_peer"]
        assert node.state.members["b"].incarnation == 2

        def fail(state):
            raise OSError("storage failure")

        monkeypatch.setattr(node.membership.store, "write", fail)

        async def updated(*args, **kwargs):
            return {"name": "lab", "members": [asdict(member("b", incarnation=3))]}

        monkeypatch.setattr(node_module, "exchange", updated)
        with pytest.raises(ClusterError, match="persistence_failed"):
            await node.synchronize(member("b").endpoint)

    asyncio.run(scenario())


def test_udp_replay_malformed_inputs_and_required_worker_failure(tmp_path, monkeypatch):
    async def scenario():
        node = make_node(tmp_path)
        node.membership.apply((member("b"),))
        node.membership.suspect(member("b"))
        inbox = DatagramInbox()
        key = uuid4().hex
        pending = PendingProbe("b", 1, asyncio.Event())
        node.pending[key] = pending
        sent = []

        async def send(peer, kind, payload):
            sent.append((kind, payload))

        monkeypatch.setattr(node, "send", send)
        async with asyncio.TaskGroup() as group:
            worker = group.create_task(node.datagrams(inbox, group))
            for kind, payload in [
                ("ack", {"id": "old" * 10 + "xx", "target": "b", "incarnation": 1}),
                ("ack", {"id": key, "target": "b", "incarnation": 2}),
                ("ack", {"id": key, "target": "c", "incarnation": 1}),
                ("gossip", {"member": asdict(member("b"))}),
                ("gossip", {}),
                ("ping", {}),
                ("response", {}),
                (
                    "ping",
                    {
                        "id": key,
                        "target": "wrong",
                        "incarnation": 1,
                        "reply": asdict(member().endpoint),
                    },
                ),
            ]:
                inbox.datagram_received(node.cipher.seal(kind, payload), ())
            await asyncio.sleep(0.02)
            assert not pending.event.is_set()
            assert node.state.members["b"].state is MemberState.SUSPECT
            assert node.last_contact is None
            inbox.datagram_received(
                node.cipher.seal("ack", {"id": key, "target": "b", "incarnation": 1}),
                (),
            )
            await asyncio.wait_for(pending.event.wait(), 1)
            assert node.state.members["b"].state is MemberState.SUSPECT
            await node.relay({"target": "missing", "incarnation": 1})
            # Saturation uses distinct tasks; no additional relay is admitted.
            blockers = [group.create_task(asyncio.Event().wait()) for _ in range(16)]
            node.relays = set(blockers)
            inbox.datagram_received(
                node.cipher.seal(
                    "ping_req",
                    {
                        "id": key,
                        "target": "b",
                        "incarnation": 1,
                        "reply": asdict(member().endpoint),
                    },
                ),
                (),
            )
            await asyncio.sleep(0.02)
            assert len(node.relays) == 16
            worker.cancel()
            for task in blockers:
                task.cancel()

        # A storage failure is fatal to the required receive loop, not silently swallowed.
        node.membership.failed = False

        def fail(_state):
            raise OSError("private disk message")

        monkeypatch.setattr(node.membership.store, "write", fail)
        inbox.datagram_received(
            node.cipher.seal("gossip", {"member": asdict(member("b", incarnation=2))}),
            (),
        )
        with pytest.RaisesGroup(ClusterError, match="unhandled errors"):
            async with asyncio.TaskGroup() as group:
                await node.datagrams(inbox, group)
        assert node.membership.failed

    asyncio.run(scenario())


def test_udp_dns_failure_is_not_removal(tmp_path, monkeypatch):
    async def scenario():
        node = make_node(tmp_path)
        node.membership.apply((member("b"),))

        async def fail(*args):
            raise OSError("DNS error")

        monkeypatch.setattr(node_module, "resolve", fail)
        await node.send(member("b").endpoint, "ping", {})
        assert node.state.members["b"] == member("b")

    asyncio.run(scenario())


def test_single_host_does_not_create_or_probe_other_members(tmp_path, monkeypatch):
    async def scenario():
        node = make_node(tmp_path)

        async def unexpected(*args, **kwargs):
            pytest.fail("a solitary member has no target")

        monkeypatch.setattr(node, "probe", unexpected)
        monkeypatch.setattr(node, "synchronize", unexpected)
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(node.probes()), group.create_task(node.sync())]
            await asyncio.sleep(0.06)
            for task in tasks:
                task.cancel()
        assert set(node.state.members) == {"a"}
        assert node.last_contact is None

    asyncio.run(scenario())
