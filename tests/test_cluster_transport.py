"""Wire rejection, replay correlation, DNS fallback and connection resource limits."""

import asyncio
import base64
import json
import os
import socket
import struct
from dataclasses import asdict
from uuid import uuid4

import pytest

from atlas.cluster import network
from atlas.cluster.model import encoded
from atlas.cluster.network import DatagramInbox, exchange, listen
from atlas.cluster.store import Credential
from atlas.cluster.wire import MAX_DATAGRAM, MAX_FRAME, Cipher, read_frame, write_frame
from atlas_core.cluster import ClusterError, PeerEndpoint

from .test_cluster_membership import member
from .test_cluster_process import free_port


def cipher():
    return Cipher(Credential(str(uuid4()), os.urandom(32)))


def test_aead_tamper_nonce_limits_and_invalid_envelopes():
    crypt = cipher()
    first = crypt.seal("gossip", {"member": asdict(member())}, limit=MAX_DATAGRAM)
    second = crypt.seal("gossip", {"member": asdict(member())}, limit=MAX_DATAGRAM)
    assert first != second
    assert crypt.open(first)[1] == {"member": asdict(member())}
    envelope = json.loads(first)
    invalid = [
        b"bad",
        b"[]",
        encoded(envelope | {"nonce": "00"}),
        encoded(envelope | {"header": [1, "different", "gossip"]}),
        encoded(envelope | {"data": "!!!!"}),
        first + b"x",
        b"x" * (MAX_FRAME + 1),
    ]
    damaged = bytearray(base64.b64decode(envelope["data"]))
    damaged[-1] ^= 1
    invalid.append(encoded(envelope | {"data": base64.b64encode(damaged).decode()}))
    for packet in invalid:
        with pytest.raises(ClusterError, match="authentication_or_format_failed"):
            crypt.open(packet)
    with pytest.raises(ClusterError, match="authentication_or_format_failed"):
        cipher().open(first)
    with pytest.raises(ClusterError, match="message_too_large"):
        crypt.seal("gossip", {"large": "x" * MAX_DATAGRAM}, limit=MAX_DATAGRAM)
    with pytest.raises(ClusterError):
        crypt.open(crypt.seal("gossip", []))


def test_frame_limits_and_udp_queue():
    async def scenario():
        for length in (0, MAX_FRAME + 1):
            reader = asyncio.StreamReader()
            reader.feed_data(struct.pack("!I", length))
            with pytest.raises(ClusterError, match="message_too_large"):
                await read_frame(reader)
        with pytest.raises(ClusterError, match="message_too_large"):
            await write_frame(None, b"x" * (MAX_FRAME + 1))
        reader = asyncio.StreamReader()
        reader.feed_data(struct.pack("!I", 10) + b"short")
        reader.feed_eof()
        with pytest.raises(asyncio.IncompleteReadError):
            await read_frame(reader)

    asyncio.run(scenario())
    inbox = DatagramInbox()
    inbox.datagram_received(b"x" * (MAX_DATAGRAM + 1), ())
    assert inbox.queue.empty()
    for _ in range(300):
        inbox.datagram_received(b"x", ())
    assert inbox.queue.qsize() == 256


def test_real_tcp_admission_replay_and_dns_reresolution(monkeypatch):
    async def scenario():
        crypt = cipher()
        peer = PeerEndpoint("127.0.0.1", free_port("127.0.0.1"))
        calls = []
        ready = [False]

        async def handler(method, data):
            calls.append((method, data))
            if method != "fetch":
                raise ClusterError("invalid_request")
            return {"members": []}

        async with listen(
            peer, lambda: crypt if ready[0] else None, handler, timeout=2
        ):
            with pytest.raises(ClusterError, match="peer_unavailable"):
                await exchange(peer, crypt, "fetch", {}, timeout=1)
            ready[0] = True
            # An unauthenticated connection gets a challenge, but cannot replay an old request.
            reader, writer = await asyncio.open_connection(peer.host, peer.port)
            _, challenge = crypt.open(await read_frame(reader))
            old = crypt.seal(
                "request",
                {
                    "token": challenge["token"],
                    "id": uuid4().hex,
                    "method": "fetch",
                    "data": {},
                },
            )
            await write_frame(writer, old)
            await read_frame(reader)
            writer.close()
            reader, writer = await asyncio.open_connection(peer.host, peer.port)
            await read_frame(reader)
            await write_frame(writer, old)
            assert await reader.read() == b""
            writer.close()
            assert len(calls) == 1
            with pytest.raises(ClusterError, match="invalid_request"):
                await exchange(peer, crypt, "bad", {}, timeout=1)

            # Bound unauthenticated sockets before task allocation.
            connections = []
            for _ in range(network.CONNECTION_LIMIT):
                reader, writer = await asyncio.open_connection(peer.host, peer.port)
                await read_frame(reader)
                connections.append((reader, writer))
            reader, writer = await asyncio.open_connection(peer.host, peer.port)
            assert await reader.read() == b""
            writer.close()
            for _, writer in connections:
                writer.close()
            await asyncio.sleep(0.05)

            resolutions = []
            original = network.resolve

            async def resolve_again(destination, socktype, family=0):
                resolutions.append(destination.host)
                good = await original(peer, socktype, family)
                bad = (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    0,
                    "",
                    ("127.0.0.1", free_port("127.0.0.1")),
                )
                return [bad, *good]

            monkeypatch.setattr(network, "resolve", resolve_again)
            for _ in range(2):
                assert await exchange(
                    PeerEndpoint("peer.example.test", peer.port),
                    crypt,
                    "fetch",
                    {},
                    timeout=1,
                ) == {"members": []}
            assert resolutions == ["peer.example.test"] * 2

            async def dns_failure(*args):
                raise OSError("private resolver body")

            monkeypatch.setattr(network, "resolve", dns_failure)
            with pytest.raises(ClusterError, match=r"^peer_unavailable$"):
                await exchange(peer, crypt, "fetch", {}, timeout=1)
            # Active incomplete connection is cancelled when the listener closes.
            reader, writer = await asyncio.open_connection(peer.host, peer.port)
            await read_frame(reader)
        assert await reader.read() == b""
        writer.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure",
    ["challenge_type", "challenge_token", "old_id", "response_type", "result"],
)
def test_authenticated_but_invalid_tcp_responses(failure):
    async def scenario():
        crypt = cipher()
        tasks = set()

        async def respond(reader, writer):
            tasks.add(asyncio.current_task())
            try:
                kind = "ack" if failure == "challenge_type" else "challenge"
                token = 1 if failure == "challenge_token" else uuid4().hex
                await write_frame(writer, crypt.seal(kind, {"token": token}))
                if failure.startswith("challenge"):
                    return
                _, request = crypt.open(await read_frame(reader))
                reply = {
                    "id": "old" if failure == "old_id" else request["id"],
                    "result": [],
                }
                await write_frame(
                    writer,
                    crypt.seal(
                        "ack" if failure == "response_type" else "response", reply
                    ),
                )
            finally:
                writer.close()
                tasks.discard(asyncio.current_task())

        async with await asyncio.start_server(respond, "127.0.0.1", 0) as server:
            peer = PeerEndpoint("127.0.0.1", server.sockets[0].getsockname()[1])
            with pytest.raises(ClusterError, match="invalid_response"):
                await exchange(peer, crypt, "fetch", {}, timeout=1)
        await asyncio.gather(*tasks)

    asyncio.run(scenario())


def test_udp_bind_conflict_closes_tcp_listener():
    async def scenario():
        peer = PeerEndpoint("127.0.0.1", free_port("127.0.0.1"))
        crypt = cipher()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as blocker:
            blocker.bind((peer.host, peer.port))
            with pytest.RaisesGroup(OSError):
                # TaskGroup propagates startup errors as an ExceptionGroup.
                async with listen(peer, lambda: crypt, None, timeout=1):
                    pytest.fail("must not start with occupied UDP port")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((peer.host, peer.port))

    asyncio.run(scenario())
