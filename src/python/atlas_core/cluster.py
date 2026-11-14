"""Typed local observations from atlasd; importing the client starts no services."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from atlas_core.ipc import Client, IPCError, Unavailable

API_VERSION = "atlas.cluster/v2"


class ClusterError(Exception):
    """A rejected request, identified by a non-secret error code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ClusterUnavailable(ClusterError):
    """The local daemon could not answer; a mutation may already have committed."""


class MemberState(StrEnum):
    UNKNOWN = "unknown"
    ALIVE = "alive"
    SUSPECT = "suspect"
    UNREACHABLE = "unreachable"
    LEFT = "left"


@dataclass(frozen=True)
class PeerEndpoint:
    host: str
    port: int


@dataclass(frozen=True)
class ClusterMember:
    id: str
    hostname: str
    endpoint: PeerEndpoint
    role: str
    site: str
    labels: dict[str, str]
    incarnation: int
    state: MemberState


@dataclass(frozen=True)
class ClusterMembers:
    """A local observation, not an authoritative or globally fresh inventory."""

    members: tuple[ClusterMember, ...]
    fetched_at: str
    observation: str = "local"


@dataclass(frozen=True)
class ClusterStatus:
    local_id: str
    cluster_id: str | None
    name: str | None
    membership: str
    last_peer_contact: str | None


def socket_path() -> Path:
    explicit = os.environ.get("ATLAS_CLUSTER_SOCKET")
    if explicit:
        return Path(explicit)
    return Path("/run/atlas/cluster/control.sock")


class ClusterClient:
    """Unary IPC client. No cached-file fallback and no cryptography dependency."""

    def __init__(self, path: Path | None = None, *, timeout: float = 15) -> None:
        self._client = Client(
            path or socket_path(), api_version=API_VERSION, timeout=timeout
        )

    @classmethod
    @asynccontextmanager
    async def connect(
        cls, path: Path | None = None, *, timeout: float = 15
    ) -> AsyncIterator[ClusterClient]:
        client = cls(path, timeout=timeout)
        await client.status()
        yield client

    async def request(self, method: str, params: dict | None = None) -> dict:
        """Issue a local control request; transport failure is not a safe retry signal."""
        try:
            return await self._client.request(method, params)
        except Unavailable:
            raise ClusterUnavailable("daemon_unavailable") from None
        except IPCError as exc:
            raise ClusterError(exc.code) from None

    async def status(self) -> ClusterStatus:
        return ClusterStatus(**await self.request("status"))

    async def members(self) -> ClusterMembers:
        result = await self.request("members")
        return ClusterMembers(
            tuple(
                ClusterMember(
                    **(
                        member
                        | {
                            "endpoint": PeerEndpoint(**member["endpoint"]),
                            "state": MemberState(member["state"]),
                        }
                    )
                )
                for member in result["members"]
            ),
            fetched_at=result["fetched_at"],
        )
