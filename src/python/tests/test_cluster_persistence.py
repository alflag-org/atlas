"""Slow durable writes must not stop probes or lose concurrent transitions."""

import asyncio
import os
import threading
from contextlib import contextmanager
from dataclasses import asdict, replace
from uuid import uuid4

import pytest

from atlas.cluster.network import DatagramInbox, exchange, listen
from atlas.cluster.node import PendingProbe
from atlas_core.cluster import (
    API_VERSION,
    ClusterClient,
    ClusterError,
    PeerEndpoint,
)
from atlas_core.ipc import serve

from .test_cluster_membership import member
from .test_cluster_node import make_node
from .test_cluster_process import free_port


@contextmanager
def blocked_fsync(monkeypatch, *, fail=False, call=1):
    """Pause a real write at a chosen fsync; always release the worker on failure."""
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    original = os.fsync
    calls = 0

    def fsync(fd):
        nonlocal calls
        calls += 1
        if calls == call:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5), "test did not release fsync"
            if fail:
                raise OSError("private storage failure")
        original(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fsync)
        try:
            yield entered, release
        finally:
            release.set()


@pytest.mark.parametrize("sync_call", [1, 2])
def test_slow_fsync_preserves_udp_tcp_and_ipc(tmp_path, monkeypatch, sync_call):
    async def scenario():
        node = await make_node(tmp_path)
        peer = PeerEndpoint("127.0.0.1", free_port("127.0.0.1"))
        (
            client_transport,
            replies,
        ) = await asyncio.get_running_loop().create_datagram_endpoint(
            DatagramInbox, local_addr=("127.0.0.1", 0)
        )
        reply = PeerEndpoint(*client_transport.get_extra_info("sockname"))
        try:
            async with (
                listen(peer, lambda: node.cipher, node.remote, timeout=1) as (
                    transport,
                    inbox,
                    family,
                ),
                serve(
                    tmp_path / "control.sock",
                    api_version=API_VERSION,
                    handler=node.control,
                    authorize=lambda *_: True,
                    timeout=1,
                ),
                asyncio.TaskGroup() as group,
            ):
                node.transport, node.family = transport, family
                tasks = [
                    group.create_task(node.datagrams(inbox, group)),
                    group.create_task(node.receive_gossip()),
                ]
                try:
                    with blocked_fsync(monkeypatch, call=sync_call) as (
                        entered,
                        release,
                    ):
                        client_transport.sendto(
                            node.cipher.seal("gossip", {"member": asdict(member("b"))}),
                            (peer.host, peer.port),
                        )
                        await asyncio.wait_for(entered.wait(), 1)
                        # Both ping replies and received ACKs bypass the blocked writer.
                        request_id = uuid4().hex
                        pending = PendingProbe("b", 1, asyncio.Event())
                        node.pending[request_id] = pending
                        for kind, payload in (
                            (
                                "ping",
                                {
                                    "id": request_id,
                                    "target": "a",
                                    "incarnation": 1,
                                    "reply": asdict(reply),
                                },
                            ),
                            (
                                "ack",
                                {"id": request_id, "target": "b", "incarnation": 1},
                            ),
                        ):
                            client_transport.sendto(
                                node.cipher.seal(kind, payload), (peer.host, peer.port)
                            )
                        packet, source = await asyncio.wait_for(replies.queue.get(), 1)
                        assert source == (peer.host, peer.port)
                        assert node.cipher.open(packet) == (
                            "ack",
                            {"id": request_id, "target": "a", "incarnation": 1},
                        )
                        await asyncio.wait_for(pending.event.wait(), 1)
                        fetched = await exchange(
                            peer,
                            node.cipher,
                            "fetch",
                            {"sender_id": "observer"},
                            timeout=1,
                        )
                        assert [m["id"] for m in fetched["members"]] == ["a"]
                        client = ClusterClient(tmp_path / "control.sock", timeout=1)
                        assert (await client.status()).membership == "joined"
                        assert [m.id for m in (await client.members()).members] == ["a"]
                        assert "b" not in node.state.members
                        release.set()
                        async with asyncio.timeout(1):
                            while "b" not in node.state.members:
                                await asyncio.sleep(0.001)
                        assert node.membership.store.read("a") == node.state
                finally:
                    for task in tasks:
                        task.cancel()
        finally:
            client_transport.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize("sync_call", [1, 2])
def test_cancelled_write_settles_before_next_update(
    tmp_path, monkeypatch, fail, sync_call
):
    async def scenario():
        node = await make_node(tmp_path)
        original = node.state
        with blocked_fsync(monkeypatch, fail=fail, call=sync_call) as (
            entered,
            release,
        ):
            first = asyncio.create_task(node.membership.apply((member("b"),)))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                second = asyncio.create_task(node.membership.apply((member("c"),)))
                queued = asyncio.create_task(node.membership.apply((member("d"),)))
                await asyncio.sleep(0)
                queued.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await queued
                first.cancel()
                await asyncio.sleep(0)
                first.cancel()  # Repeated shutdown/timeout cancellation must also wait.
                await asyncio.sleep(0)
                assert not first.done() and not second.done()
                assert node.state == original and node.membership.lock.locked()
            finally:
                release.set()
            results = await asyncio.gather(first, second, return_exceptions=True)
        if fail:
            assert all(isinstance(result, ClusterError) for result in results)
            assert [result.code for result in results] == ["persistence_failed"] * 2
            assert node.state == original and node.membership.failed
            with pytest.raises(ClusterError, match="persistence_failed"):
                await node.control("members", {})
        else:
            assert isinstance(results[0], asyncio.CancelledError)
            assert results[1] is None
            assert set(node.state.members) == {"a", "b", "c"}
            assert node.membership.store.read("a") == node.state

    asyncio.run(scenario())


def test_cancelled_init_finishes_credentials_and_publication(tmp_path, monkeypatch):
    async def scenario():
        node = await make_node(tmp_path, initialized=False)
        with blocked_fsync(monkeypatch) as (entered, release):
            initialize = asyncio.create_task(node.control("init", {"name": "lab"}))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                initialize.cancel()
                await asyncio.sleep(0)
                assert not initialize.done()
                assert (await node.control("status", {}))[
                    "membership"
                ] == "uninitialized"
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await initialize
        assert node.state.membership == "joined"
        assert node.cipher.cluster_id == node.state.cluster_id
        assert node.membership.store.read("a") == node.state

    asyncio.run(scenario())


def test_join_timeout_during_write_keeps_queryable_committed_state(
    tmp_path, monkeypatch
):
    async def scenario():
        node = await make_node(tmp_path)
        methods = []

        async def exchange(*args, **kwargs):
            methods.append(args[2])
            return {"sender_id": "b", "name": "lab", "members": [asdict(member("b"))]}

        monkeypatch.setattr("atlas.cluster.node.exchange", exchange)
        with blocked_fsync(monkeypatch) as (entered, release):
            joining = asyncio.create_task(
                node.control("join", {"seeds": ["127.0.0.2"]})
            )
            try:
                await asyncio.wait_for(entered.wait(), 1)
                await asyncio.sleep(node.timers.join_timeout * 2)
                assert not joining.done()
                assert [
                    m["id"] for m in (await node.control("members", {}))["members"]
                ] == ["a"]
            finally:
                release.set()
            with pytest.raises(ClusterError, match="join_timeout"):
                await joining
        assert methods == ["fetch"]
        assert set(node.state.members) == {"a", "b"}
        assert node.membership.store.read("a") == node.state
        assert node.cipher.cluster_id == node.state.cluster_id

    asyncio.run(scenario())


def test_concurrent_leave_and_stale_suspicion_merge_latest_state(tmp_path, monkeypatch):
    async def scenario():
        node = await make_node(tmp_path)
        await node.membership.apply((member("b"),))
        with blocked_fsync(monkeypatch) as (entered, release):
            updating = asyncio.create_task(
                node.membership.apply((member("b", incarnation=2),))
            )
            try:
                await asyncio.wait_for(entered.wait(), 1)
                leaving = asyncio.create_task(node.leave())
                suspecting = asyncio.create_task(node.membership.suspect(member("b")))
                await asyncio.sleep(0)
                assert not leaving.done() and not suspecting.done()
            finally:
                release.set()
            await asyncio.gather(updating, leaving, suspecting)
        assert node.state.membership == "left"
        assert node.state.members["b"] == member("b", incarnation=2)
        assert node.membership.store.read("a") == node.state

    asyncio.run(scenario())


def test_gossip_overflow_keeps_ack_path_and_conflicts_do_not_stop_worker(tmp_path):
    async def scenario():
        node = await make_node(tmp_path)
        inbox = DatagramInbox()
        for _ in range(node.incoming.maxsize):
            node.incoming.put_nowait(member("b"))
        request_id = uuid4().hex
        pending = PendingProbe("b", 1, asyncio.Event())
        node.pending[request_id] = pending
        inbox.datagram_received(
            node.cipher.seal("gossip", {"member": asdict(member("c"))}), ()
        )
        inbox.datagram_received(
            node.cipher.seal(
                "ack", {"id": request_id, "target": "b", "incarnation": 1}
            ),
            (),
        )
        async with asyncio.TaskGroup() as group:
            receiver = group.create_task(node.datagrams(inbox, group))
            await asyncio.wait_for(pending.event.wait(), 1)
            assert node.incoming.qsize() == node.incoming.maxsize
            worker = group.create_task(node.receive_gossip())
            async with asyncio.timeout(1):
                while not node.incoming.empty():
                    await asyncio.sleep(0.001)
            node.incoming.put_nowait(
                replace(member("b"), labels={"conflict": "same-generation"})
            )
            node.incoming.put_nowait(member("b", incarnation=2))
            async with asyncio.timeout(1):
                while node.state.members["b"].incarnation != 2:
                    await asyncio.sleep(0.001)
            assert "c" not in node.state.members
            assert not node.membership.failed
            receiver.cancel()
            worker.cancel()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("initializing", "sync_call"), [(False, 1), (True, 1), (True, 3)]
)
def test_blanket_cancellation_waits_for_inner_durable_operation(
    tmp_path, monkeypatch, initializing, sync_call
):
    async def scenario():
        node = await make_node(tmp_path, initialized=not initializing)
        existing = asyncio.all_tasks()
        with blocked_fsync(monkeypatch, call=sync_call) as (entered, release):
            operation = asyncio.create_task(
                node.initialize("lab")
                if initializing
                else node.membership.apply((member("b"),))
            )
            try:
                await asyncio.wait_for(entered.wait(), 1)
                tasks = asyncio.all_tasks() - existing
                for task in tasks:
                    task.cancel()
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                assert not operation.done()
                assert node.membership.lock.locked()
            finally:
                release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
        assert operation.cancelled()
        assert node.state.membership == "joined"
        assert node.membership.store.read("a") == node.state
        if initializing:
            assert node.cipher.cluster_id == node.state.cluster_id
        else:
            await node.membership.apply((member("c"),))
            assert set(node.state.members) == {"a", "b", "c"}

    asyncio.run(scenario())
