"""Owner-only credentials and atomic durable membership snapshots."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

from atlas_core.cluster import ClusterError, ClusterMember, MemberState, PeerEndpoint

from .model import encoded, parse_endpoint, records, text

MAX_BYTES = 1024 * 1024
SCHEMA = "atlas.gossip-state/v1"


def private_file(path: Path, flags: int) -> int:
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        os.close(fd)
        raise ClusterError("unsafe_state")
    return fd


def read_json(path: Path) -> dict:
    with os.fdopen(private_file(path, os.O_RDONLY), "rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    try:
        value = json.loads(data)
        if len(data) > MAX_BYTES or not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, RecursionError):
        raise ClusterError("invalid_state") from None


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@dataclass(frozen=True)
class Credential:
    cluster_id: str
    key: bytes = field(repr=False)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.key).hexdigest()

    @classmethod
    def read(cls, path: Path) -> Credential:
        raw = read_json(path)
        try:
            if set(raw) != {"cluster_id", "key"}:
                raise ValueError
            cluster_id = str(UUID(raw["cluster_id"]))
            key = bytes.fromhex(raw["key"])
            if len(key) != 32:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ClusterError("invalid_credentials") from None
        return cls(cluster_id, key)

    @classmethod
    def create(cls, path: Path) -> Credential:
        credential = cls(str(uuid4()), os.urandom(32))
        with os.fdopen(
            private_file(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "wb"
        ) as stream:
            stream.write(
                encoded(
                    {"cluster_id": credential.cluster_id, "key": credential.key.hex()}
                )
            )
            stream.flush()
            os.fsync(stream.fileno())
        sync_directory(path.parent)
        return credential


@dataclass(frozen=True)
class State:
    local_id: str
    cluster_id: str | None = None
    name: str | None = None
    membership: str = "uninitialized"
    members: dict[str, ClusterMember] = field(default_factory=dict)
    seeds: tuple[PeerEndpoint, ...] = ()
    credential_fingerprint: str | None = None

    def document(self) -> dict:
        return {
            "schema": SCHEMA,
            "credential_fingerprint": self.credential_fingerprint,
            "local_id": self.local_id,
            "cluster_id": self.cluster_id,
            "name": self.name,
            "membership": self.membership,
            "members": [asdict(item) for item in self.members.values()],
            "seeds": [asdict(peer) for peer in self.seeds],
        }


class Store:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.path = directory / "membership.json"

    @contextmanager
    def locked(self) -> Iterator[None]:
        fd = private_file(self.directory / ".lock", os.O_CREAT | os.O_RDWR)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ClusterError("already_running") from None
            yield
        finally:
            os.close(fd)

    def read(self, local_id: str) -> State:
        try:
            raw = read_json(self.path)
        except FileNotFoundError:
            return State(local_id)
        if raw.get("schema") == "atlas.membership/v1":
            raise ClusterError("legacy_state_requires_reconfiguration")
        try:
            if set(raw) != {
                "schema",
                "credential_fingerprint",
                "local_id",
                "cluster_id",
                "name",
                "membership",
                "members",
                "seeds",
            }:
                raise ValueError
            if raw["schema"] != SCHEMA or raw["local_id"] != local_id:
                raise ValueError
            cluster_id = str(UUID(raw["cluster_id"]))
            fingerprint = raw["credential_fingerprint"]
            if (
                not isinstance(fingerprint, str)
                or len(bytes.fromhex(fingerprint)) != 32
            ):
                raise ValueError
            name = text(raw["name"])
            if raw["membership"] not in {"joined", "left"}:
                raise ValueError
            members = {item.id: item for item in records(raw["members"])}
            own = members[local_id]
            if (own.state is MemberState.LEFT) != (raw["membership"] == "left"):
                raise ValueError
            if not isinstance(raw["seeds"], list) or len(raw["seeds"]) > 32:
                raise ValueError
            seeds = tuple(parse_endpoint(peer) for peer in raw["seeds"])
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ClusterError("invalid_state") from None
        return State(
            local_id, cluster_id, name, raw["membership"], members, seeds, fingerprint
        )

    def write(self, state: State) -> None:
        data = encoded(state.document())
        if len(data) > MAX_BYTES:
            raise ClusterError("membership_too_large")
        fd, temporary = tempfile.mkstemp(dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            sync_directory(self.directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
                sync_directory(self.directory)
