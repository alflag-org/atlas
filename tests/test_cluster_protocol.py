from __future__ import annotations

import asyncio
import builtins
from uuid import uuid4

import pytest

from atlas.cluster import identity, network
from atlas_core.cluster import ClusterClient, ClusterUnavailable
from atlas_core.ipc import IPCError, read_message, serve, write_message

from .test_cluster_membership import address


def test_optional_dependency_and_certificate_constraints(monkeypatch):
    for value in (
        "http://127.0.0.1:44",
        "tls://host.example:44",
        "tls://127.0.0.1:4/path",
        "tls://127.0.0.1:99999",
        "tls://user@127.0.0.1:4",
    ):
        with pytest.raises(IPCError, match="invalid_endpoint"):
            identity.endpoint(value)
    x509, serialization, _ = identity.crypto()
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec

    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([]))
        .sign(ec.generate_private_key(ec.SECP256R1()), hashes.SHA256())
    )
    with pytest.raises(IPCError, match="invalid_identity"):
        identity.issue(
            identity.new_key(),
            str(uuid4()),
            csr=csr.public_bytes(serialization.Encoding.PEM).decode(),
            node_id=str(uuid4()),
        )
    original = builtins.__import__

    def without_crypto(name, *args, **kwargs):
        if name == "cryptography":
            raise ImportError
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_crypto)
    with pytest.raises(IPCError, match="cluster_dependency_missing"):
        identity.new_key()


def test_cluster_client_maps_authority_unavailability(tmp_path):
    async def scenario():
        async def unavailable(method, params, peer):
            raise IPCError("unavailable")

        path = tmp_path / "control.sock"
        async with serve(
            path,
            api_version="atlas.cluster/v1",
            handler=unavailable,
            authorize=lambda p, m: True,
        ):
            with pytest.raises(ClusterUnavailable):
                await ClusterClient(path).status()

    asyncio.run(scenario())


def test_network_request_validation_and_bounded_shutdown(tmp_path):
    async def scenario():
        ca_key, key = identity.new_key(), identity.new_key()
        node_id, cluster_id = str(uuid4()), str(uuid4())
        ca = identity.issue(ca_key, cluster_id)
        cert = identity.issue(
            ca_key,
            cluster_id,
            csr=identity.request_identity(key, node_id),
            node_id=node_id,
        )
        server_context = identity.tls_context(
            tmp_path, ca, key=key, certificate=cert, server=True
        )
        client_context = identity.tls_context(tmp_path, ca)
        endpoint = address()
        host, port = identity.endpoint(endpoint)
        entered = asyncio.Event()

        async def handler(method, params, peer):
            if method == "wait":
                entered.set()
                await asyncio.Event().wait()
            raise ValueError("private diagnostic")

        listener = network.Listener(handler)
        await listener.start(endpoint, server_context)
        try:
            for message, error in (
                ({"request_id": "id", "api_version": "wrong"}, "unsupported_version"),
                (
                    {
                        "request_id": "id",
                        "api_version": network.VERSION,
                        "method": 1,
                        "params": {},
                    },
                    "invalid_message",
                ),
            ):
                reader, writer = await asyncio.open_connection(
                    host, port, ssl=client_context, server_hostname=f"{node_id}.invalid"
                )
                await write_message(writer, message)
                assert (await read_message(reader))["error"] == error
                writer.close()
                await writer.wait_closed()
            reader, writer = await asyncio.open_connection(
                host, port, ssl=client_context, server_hostname=f"{node_id}.invalid"
            )
            await write_message(writer, {"request_id": None})
            assert await reader.read() == b""
            writer.close()
            await writer.wait_closed()
            with pytest.raises(IPCError, match="internal_error"):
                await network.call(endpoint, node_id, client_context, "fail", {})
            waiting = asyncio.create_task(
                network.call(endpoint, node_id, client_context, "wait", {})
            )
            await entered.wait()
            async with asyncio.timeout(1):
                await listener.close()
            with pytest.raises(IPCError, match="unavailable"):
                await waiting
        finally:
            await listener.close()
        # Misbehaving peer replies cannot be mistaken for successful results.
        response = {}

        async def fake(reader, writer):
            request = await read_message(reader)
            await write_message(writer, request | response)
            writer.close()

        async with await asyncio.start_server(fake, host, port, ssl=server_context):
            for value in ({"request_id": "wrong"}, {"result": []}):
                response.clear()
                response.update(value)
                with pytest.raises(IPCError, match="invalid_response"):
                    await network.call(endpoint, node_id, client_context, "status", {})
        empty = network.Listener(handler)
        await empty.close()

    asyncio.run(scenario())


def test_network_rejects_excess_connections(tmp_path):
    async def scenario():
        ca_key, key, node_id, cluster_id = (
            identity.new_key(),
            identity.new_key(),
            str(uuid4()),
            str(uuid4()),
        )
        ca = identity.issue(ca_key, cluster_id)
        cert = identity.issue(
            ca_key,
            cluster_id,
            csr=identity.request_identity(key, node_id),
            node_id=node_id,
        )
        context = identity.tls_context(
            tmp_path, ca, key=key, certificate=cert, server=True
        )
        client = identity.tls_context(tmp_path, ca)
        listener = network.Listener(lambda *args: None)
        endpoint = address()
        await listener.start(endpoint, context)
        connections = []
        try:
            for _ in range(33):
                connections.append(
                    await asyncio.open_connection(
                        *identity.endpoint(endpoint),
                        ssl=client,
                        server_hostname=f"{node_id}.invalid",
                    )
                )
            async with asyncio.timeout(2):
                assert await connections[-1][0].read() == b""
        finally:
            for _, writer in connections:
                writer.close()
            await listener.close()

    asyncio.run(scenario())
