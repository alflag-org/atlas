"""Typed local client for an explicitly started Atlas Cluster daemon."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from .ipc import Client, IPCError, Unavailable


class ClusterError(IPCError):
    """A Cluster request was rejected, with a stable ``code``."""


class ClusterUnavailable(ClusterError):
    """The local daemon or authority cannot be reached; mutations may be unknown."""


@dataclass(frozen=True)
class NodeInfo:
    node_id: str
    display_name: str
    endpoint: str
    state: str
    permissions: tuple[str, ...]
    labels: dict[str, str]


@dataclass(frozen=True)
class ClusterStatus:
    cluster_id: str | None
    name: str | None
    node_id: str
    authority_id: str | None
    revision: int
    stale: bool


class ClusterClient:
    """Call the local daemon without starting it or importing Cluster dependencies."""

    def __init__(self, path: Path | None = None, *, timeout: float = 30) -> None:
        self._client = Client(
            path
            or Path(
                os.environ.get("ATLAS_CLUSTER_SOCKET", "/run/atlas/atlasd/control.sock")
            ),
            api_version="atlas.cluster/v1",
            timeout=timeout,
        )

    @classmethod
    @asynccontextmanager
    async def connect(
        cls, path: Path | None = None, *, timeout: float = 30
    ) -> AsyncIterator[ClusterClient]:
        """Check availability, then provide a client with no persistent connection."""
        client = cls(path, timeout=timeout)
        await client.status()
        yield client

    async def request(self, method: str, params: dict | None = None) -> dict:
        """Issue a protocol operation once; never retry a lost response."""
        try:
            return await self._client.request(method, params)
        except Unavailable:
            raise ClusterUnavailable("unavailable") from None
        except IPCError as exc:
            if exc.code == "unavailable":
                raise ClusterUnavailable(exc.code) from None
            raise ClusterError(exc.code) from None

    async def status(self) -> ClusterStatus:
        return ClusterStatus(**await self.request("status"))

    async def members(self) -> tuple[NodeInfo, ...]:
        result = await self.request("members")
        return tuple(
            NodeInfo(**(item | {"permissions": tuple(item["permissions"])}))
            for item in result["members"]
        )
