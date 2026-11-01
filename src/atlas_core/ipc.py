"""Bounded unary JSON RPC over private Linux Unix sockets.

Applications own their API versions, methods, data models and authorization rules.
A connection carries one request. Transport failure never proves that a mutation
was not performed; callers must reconcile rather than retry blindly.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import math
import os
import socket
import stat
import struct
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

MAX_MESSAGE_BYTES = 1024 * 1024


class IPCError(Exception):
    """An explicit protocol or application error with a stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class Unavailable(IPCError):
    """Connection failed or its outcome is unknown; no automatic retries occur."""

    def __init__(self) -> None:
        super().__init__("unavailable")


@dataclass(frozen=True)
class Peer:
    """Linux credentials of the process connected to a local socket."""

    pid: int
    uid: int
    gid: int


async def read_message(reader: asyncio.StreamReader) -> dict:
    """Read one length-prefixed object, rejecting oversized or malformed JSON."""
    length = struct.unpack("!I", await reader.readexactly(4))[0]
    if not 0 < length <= MAX_MESSAGE_BYTES:
        raise IPCError("invalid_message")
    try:
        message = json.loads(await reader.readexactly(length))
    except (ValueError, UnicodeError, RecursionError):
        raise IPCError("invalid_message") from None
    if not isinstance(message, dict):
        raise IPCError("invalid_message")
    return message


async def write_message(writer: asyncio.StreamWriter, message: dict) -> None:
    """Write one bounded JSON object without permitting non-finite numbers."""
    payload = json.dumps(message, allow_nan=False, separators=(",", ":")).encode()
    if len(payload) > MAX_MESSAGE_BYTES:
        raise IPCError("message_too_large")
    writer.write(struct.pack("!I", len(payload)) + payload)
    await writer.drain()


class Client:
    """One request per connection, with a deadline covering connection and reply."""

    def __init__(self, path: Path, *, api_version: str, timeout: float = 10) -> None:
        if timeout <= 0 or not math.isfinite(timeout):
            raise ValueError("IPC timeout must be finite and positive")
        self.path = path
        self.api_version = api_version
        self.timeout = timeout

    async def request(self, method: str, params: dict | None = None) -> dict:
        request_id = str(uuid4())
        try:
            async with asyncio.timeout(self.timeout):
                reader, writer = await asyncio.open_unix_connection(self.path)
                try:
                    await write_message(
                        writer,
                        {
                            "api_version": self.api_version,
                            "request_id": request_id,
                            "method": method,
                            "params": {} if params is None else params,
                        },
                    )
                    reply = await read_message(reader)
                finally:
                    writer.close()
                    await writer.wait_closed()
        except (OSError, TimeoutError, asyncio.IncompleteReadError):
            raise Unavailable() from None
        if (
            reply.get("api_version") != self.api_version
            or reply.get("request_id") != request_id
        ):
            raise IPCError("invalid_response")
        if "error" in reply:
            raise IPCError(str(reply["error"]))
        if not isinstance(reply.get("result"), dict):
            raise IPCError("invalid_response")
        return reply["result"]


Handler = Callable[[str, dict, Peer], Awaitable[dict]]
Authorize = Callable[[Peer, str], bool]


@asynccontextmanager
async def serve(
    path: Path,
    *,
    api_version: str,
    handler: Handler,
    authorize: Authorize,
    timeout: float = 10,
    max_connections: int = 32,
) -> AsyncIterator[None]:
    """Serve unary calls from a private directory; check authorization on each call.

    The caller creates an owner-only runtime directory. A process lock protects
    stale-socket cleanup across restarts. Pending calls are cancelled on close;
    applications must durably record mutations before performing side effects.
    """
    info = path.parent.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ValueError("IPC socket requires an owner-only runtime directory")
    if not path.is_absolute() or timeout <= 0 or max_connections < 1:
        raise ValueError("invalid IPC server options")
    lock = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    tasks: set[asyncio.Task] = set()

    async def connection(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        tasks.add(task)
        try:
            async with asyncio.timeout(timeout):
                if len(tasks) > max_connections:
                    return
                credentials = writer.get_extra_info("socket").getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, 12
                )
                peer = Peer(*struct.unpack("3i", credentials))
                request = await read_message(reader)
                request_id = request.get("request_id")
                if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
                    raise IPCError("invalid_message")
                reply = {
                    "api_version": request.get("api_version"),
                    "request_id": request_id,
                }
                try:
                    if request.get("api_version") != api_version:
                        raise IPCError("unsupported_version")
                    method, params = request.get("method"), request.get("params")
                    if not isinstance(method, str) or not isinstance(params, dict):
                        raise IPCError("invalid_message")
                    if not authorize(peer, method):
                        raise IPCError("forbidden")
                    reply["result"] = await handler(method, params, peer)
                except IPCError as exc:
                    reply["error"] = exc.code
                except Exception:
                    # Never reflect handler/provider exception bodies to a client.
                    reply["error"] = "internal_error"
                await write_message(writer, reply)
        except (OSError, TimeoutError, IPCError, asyncio.IncompleteReadError):
            pass  # A broken or malformed connection has no response channel.
        finally:
            tasks.remove(task)
            writer.close()

    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if path.exists() or path.is_symlink():
            if not stat.S_ISSOCK(path.lstat().st_mode):
                raise ValueError("IPC endpoint is not a socket")
            path.unlink()
        server = await asyncio.start_unix_server(connection, path=path)
        try:
            path.chmod(0o600)
            yield
        finally:
            server.close()
            path.unlink(missing_ok=True)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await server.wait_closed()
    finally:
        os.close(lock)
