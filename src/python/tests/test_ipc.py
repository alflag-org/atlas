from __future__ import annotations

import asyncio
import importlib
import os
import struct
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from atlas_core.ipc import (
    MAX_MESSAGE_BYTES,
    Client,
    IPCError,
    Unavailable,
    read_message,
    serve,
    write_message,
)


def test_service_owns_api_and_checks_each_operation(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(
        str(Path(__file__).resolve().parents[1] / "examples/service")
    )
    api = importlib.import_module("sample_service.api")
    application = importlib.import_module("sample_service.__main__")

    async def scenario():
        path = tmp_path / "control.sock"
        seen = []

        def authorize(peer, method):
            seen.append((peer.uid, method))
            return method != "forbidden"

        async with serve(
            path,
            api_version="sample.service/v1",
            handler=application.status,
            authorize=authorize,
        ):
            assert path.stat().st_mode & 0o777 == 0o600
            assert (await api.SampleClient(path).status()).message == "ready"
            for method, code in (
                ("forbidden", "forbidden"),
                ("unknown", "unknown_method"),
            ):
                with pytest.raises(IPCError, match=code):
                    await Client(path, api_version="sample.service/v1").request(method)
            with pytest.raises(IPCError, match="unsupported_version"):
                await Client(path, api_version="sample.service/v2").request("status")
            with pytest.raises(BlockingIOError):
                async with serve(
                    path,
                    api_version="other",
                    handler=application.status,
                    authorize=authorize,
                ):
                    pytest.fail("second service must not replace the live socket")
        assert seen == [
            (os.getuid(), "status"),
            (os.getuid(), "forbidden"),
            (os.getuid(), "unknown"),
        ]
        assert not path.exists()
        with pytest.raises(api.SampleError, match="unavailable"):
            await api.SampleClient(path).status()
        with pytest.raises(Unavailable):
            await Client(path, api_version="sample.service/v1").request("status")

    asyncio.run(scenario())


def test_message_limits_and_malformed_frames():
    async def scenario():
        for payload in (b"not-json", b"[]", b"\xff", b"[" * 10000):
            reader = asyncio.StreamReader()
            reader.feed_data(struct.pack("!I", len(payload)) + payload)
            with pytest.raises(IPCError, match="invalid_message"):
                await read_message(reader)
        for size in (0, MAX_MESSAGE_BYTES + 1):
            reader = asyncio.StreamReader()
            reader.feed_data(struct.pack("!I", size))
            with pytest.raises(IPCError, match="invalid_message"):
                await read_message(reader)
        with pytest.raises(IPCError, match="message_too_large"):
            await write_message(AsyncMock(), {"data": "x" * MAX_MESSAGE_BYTES})

    asyncio.run(scenario())


def test_peer_cannot_hang_server_or_receive_exception_bodies(tmp_path):
    async def scenario():
        path = tmp_path / "control.sock"

        async def handler(method, params, peer):
            if method == "fail":
                raise ValueError("secret provider body")
            await asyncio.Event().wait()

        async with serve(
            path,
            api_version="test/v1",
            handler=handler,
            authorize=lambda p, m: True,
            timeout=0.05,
            max_connections=1,
        ):
            with pytest.raises(IPCError, match="internal_error") as error:
                await Client(path, api_version="test/v1").request("fail")
            assert "secret" not in str(error.value)
            with pytest.raises(Unavailable):
                await Client(path, api_version="test/v1", timeout=0.1).request("wait")
            first_reader, first_writer = await asyncio.open_unix_connection(path)
            await asyncio.sleep(0)
            reader, writer = await asyncio.open_unix_connection(path)
            assert await reader.read() == b""
            writer.close()
            first_writer.close()
            await first_reader.read()
            reader, writer = await asyncio.open_unix_connection(path)
            await write_message(writer, {"request_id": 1})
            assert await reader.read() == b""
            writer.close()
            reader, writer = await asyncio.open_unix_connection(path)
            await write_message(
                writer,
                {
                    "request_id": "id",
                    "api_version": "test/v1",
                    "method": [],
                    "params": {},
                },
            )
            assert (await read_message(reader))["error"] == "invalid_message"
            writer.close()
        # Recovery removes only an abandoned socket, never an unrelated file.
        import socket

        sock = socket.socket(socket.AF_UNIX)
        sock.bind(str(path))
        sock.close()
        async with serve(
            path, api_version="test/v1", handler=handler, authorize=lambda p, m: True
        ):
            reader, writer = await asyncio.open_unix_connection(path)
            await asyncio.sleep(0.01)
        writer.close()
        assert await reader.read() == b""
        path.write_text("keep")
        with pytest.raises(ValueError, match="not a socket"):
            async with serve(
                path,
                api_version="test/v1",
                handler=handler,
                authorize=lambda p, m: True,
            ):
                pytest.fail("must preserve unrelated endpoint")
        assert path.read_text() == "keep"
        for options in ({"timeout": 0}, {"max_connections": 0}):
            with pytest.raises(ValueError, match="options"):
                async with serve(
                    path,
                    api_version="v",
                    handler=handler,
                    authorize=lambda p, m: True,
                    **options,
                ):
                    pytest.fail("invalid server options")
        tmp_path.chmod(0o755)
        with pytest.raises(ValueError, match="owner-only"):
            async with serve(
                path, api_version="v", handler=handler, authorize=lambda p, m: True
            ):
                pytest.fail("public socket directory")

    asyncio.run(scenario())


def test_client_rejects_mismatched_or_malformed_responses(tmp_path):
    async def scenario():
        path = tmp_path / "control.sock"
        response = {}

        async def connection(reader, writer):
            request = await read_message(reader)
            await write_message(writer, request | response)
            writer.close()

        async with await asyncio.start_unix_server(connection, path=path):
            client = Client(path, api_version="test/v1")
            for value in (
                {"request_id": "wrong"},
                {"api_version": "wrong"},
                {"result": []},
            ):
                response.clear()
                response.update(value)
                with pytest.raises(IPCError, match="invalid_response"):
                    await client.request("status", {"key": "value"})
        for timeout in (0, float("inf")):
            with pytest.raises(ValueError):
                Client(path, api_version="v", timeout=timeout)

    asyncio.run(scenario())
