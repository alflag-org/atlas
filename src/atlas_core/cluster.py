"""Membership and host metadata, without application execution policy."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path


class ClusterError(Exception):
    """A rejected request or invalid state, identified by a stable ``code``."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ClusterUnavailable(ClusterError):
    """SSH could not complete the request; a membership change may have committed."""


@dataclass(frozen=True)
class ClusterMember:
    id: str
    hostname: str
    address: str
    role: str
    site: str
    labels: dict[str, str]


@dataclass(frozen=True)
class ClusterMembers:
    """An iterable snapshot; ``stale`` indicates an unrefreshed local copy."""

    members: tuple[ClusterMember, ...]
    stale: bool
    fetched_at: str

    def __iter__(self) -> Iterator[ClusterMember]:
        return iter(self.members)


@dataclass(frozen=True)
class ClusterStatus:
    cluster_id: str | None
    name: str | None
    member_id: str
    joined: bool
    registry: str | None
    registry_reachable: bool | None
    stale: bool
    fetched_at: str | None


class ClusterClient:
    """Read membership or refresh it over SSH, without starting a daemon.

    The default directory is ``$XDG_STATE_HOME/atlas/cluster`` (falling back to
    ``~/.local/state/atlas/cluster``). ``cached=True`` skips remote requests.
    Importing this module performs no I/O.
    """

    def __init__(self, directory: Path | None = None, *, timeout: float = 10) -> None:
        from atlas.cluster.node import Node

        self._node = Node(directory, timeout=timeout)

    @classmethod
    @asynccontextmanager
    async def connect(
        cls, directory: Path | None = None, *, timeout: float = 10
    ) -> AsyncIterator[ClusterClient]:
        """Provide a client; individual reads determine freshness and availability."""
        yield cls(directory, timeout=timeout)

    async def status(self, *, cached: bool = False) -> ClusterStatus:
        return ClusterStatus(**await self._node.status(cached=cached))

    async def members(self, *, cached: bool = False) -> ClusterMembers:
        snapshot, reachable = await self._node.snapshot(cached=cached)
        return ClusterMembers(
            tuple(ClusterMember(**member) for member in snapshot["members"]),
            stale=reachable is not True,
            fetched_at=snapshot["fetched_at"],
        )
