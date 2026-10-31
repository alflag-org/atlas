"""Membership registry and local cache; applications own all execution policy."""

from __future__ import annotations

import math
import socket
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from atlas_core.cluster import ClusterError, ClusterMember, ClusterUnavailable
from atlas_core.host import get_host

from . import network
from .store import Store


def text(value: object, *, empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 512
        or (not value and not empty)
        or any(ord(c) < 32 for c in value)
    ):
        raise ClusterError("invalid_metadata")
    return value


def member(value: dict) -> dict:
    try:
        parsed = ClusterMember(**value)
        for key in ("id", "hostname", "address", "role", "site"):
            text(getattr(parsed, key), empty=key in {"role", "site"})
        network.destination(parsed.address)
        if not isinstance(parsed.labels, dict) or len(parsed.labels) > 64:
            raise ValueError
        for key, label in parsed.labels.items():
            text(key)
            text(label, empty=True)
        return asdict(parsed)
    except (TypeError, ValueError):
        raise ClusterError("invalid_metadata") from None


def local_member(address: str | None, labels: dict[str, str]) -> dict:
    host = get_host()
    return member(
        dict(
            id=host.id,
            hostname=socket.gethostname(),
            address=address or socket.getfqdn(),
            role=host.role,
            site=host.site,
            labels=labels,
        )
    )


def validate_snapshot(value: dict) -> dict:
    try:
        text(value["cluster_id"])
        text(value["name"])
        text(value["registry_id"])
        datetime.fromisoformat(value["fetched_at"])
        if type(value["revision"]) is not int or value["revision"] < 1:
            raise ValueError
        members = [member(item) for item in value["members"]]
        identifiers = {item["id"] for item in members}
        if len(identifiers) != len(members) or value["registry_id"] not in identifiers:
            raise ValueError
        return value
    except (KeyError, TypeError, ValueError, ClusterError):
        raise ClusterError("invalid_snapshot") from None


def now() -> str:
    return datetime.now(UTC).isoformat()


class Node:
    def __init__(self, directory: Path | None = None, *, timeout: float = 10) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        self.store = Store(directory)
        self.timeout = timeout

    def state(self) -> dict:
        value = self.store.read()
        if value is None:
            raise ClusterError("not_joined")
        try:
            member(value["member"])
            if value["registry"] is not None:
                network.destination(value["registry"])
            if value["snapshot"] is not None:
                validate_snapshot(value["snapshot"])
            elif value["registry"] is None:
                raise ValueError
            return value
        except (KeyError, TypeError, ValueError, ClusterError):
            raise ClusterError("invalid_state") from None

    def initialize(self, name: str, metadata: dict) -> dict:
        text(name)
        metadata = member(metadata)
        snapshot = dict(
            cluster_id=str(uuid4()),
            name=name,
            registry_id=metadata["id"],
            revision=1,
            members=[metadata],
            fetched_at=now(),
        )
        with self.store.locked():
            if self.store.read() is not None:
                raise ClusterError("already_joined")
            self.store.write(
                dict(
                    schema="atlas.membership/v1",
                    member=metadata,
                    registry=None,
                    snapshot=snapshot,
                )
            )
        return snapshot

    def registry_request(self, request: dict) -> dict:
        """Run only as an OS account permitted to access this private registry."""
        with self.store.locked():
            state = self.state()
            if state["registry"] is not None:
                raise ClusterError("not_registry")
            snapshot = state["snapshot"]
            if request.get("cluster_id") not in (None, snapshot["cluster_id"]):
                raise ClusterError("cluster_changed")
            method = request.get("method")
            if method == "join":
                addition = member(request.get("member"))
                existing = next(
                    (
                        item
                        for item in snapshot["members"]
                        if item["id"] == addition["id"]
                    ),
                    None,
                )
                if existing is not None and existing != addition:
                    raise ClusterError("member_conflict")
                if existing is None:
                    snapshot["members"].append(addition)
                    snapshot["revision"] += 1
            elif method == "leave":
                identifier = text(request.get("member_id"))
                if identifier == snapshot["registry_id"]:
                    raise ClusterError("cannot_remove_registry")
                remaining = [
                    item for item in snapshot["members"] if item["id"] != identifier
                ]
                if remaining != snapshot["members"]:
                    snapshot["members"] = remaining
                    snapshot["revision"] += 1
            elif method != "members":
                raise ClusterError("unknown_method")
            snapshot["members"].sort(key=lambda item: item["id"])
            snapshot["fetched_at"] = now()
            if method != "members":
                self.store.write(state)
            return snapshot

    async def join(self, via: str, metadata: dict) -> dict:
        network.destination(via)
        metadata = member(metadata)
        pending = dict(
            schema="atlas.membership/v1", registry=via, member=metadata, snapshot=None
        )
        with self.store.locked():
            existing = self.store.read()
            if existing is not None and existing != pending:
                raise ClusterError("already_joined")
            # Retain the exact registration across a lost SSH response.
            self.store.write(pending)
        snapshot = await network.request(
            via, {"method": "join", "member": metadata}, timeout=self.timeout
        )
        validate_snapshot(snapshot)
        if metadata not in snapshot["members"]:
            raise ClusterError("invalid_snapshot")
        return self.cache(pending, snapshot)

    def cache(self, original: dict, snapshot: dict) -> dict:
        validate_snapshot(snapshot)
        with self.store.locked():
            current = self.state()
            if (
                current["registry"] != original["registry"]
                or current["member"] != original["member"]
            ):
                raise ClusterError("state_changed")
            previous = current["snapshot"]
            if previous is None and current["member"] not in snapshot["members"]:
                # A pending join must remain retryable until registration is observed.
                return snapshot
            if previous is not None:
                if previous["cluster_id"] != snapshot["cluster_id"]:
                    raise ClusterError("cluster_changed")
                if previous["revision"] > snapshot["revision"]:
                    return previous
            current["snapshot"] = snapshot
            self.store.write(current)
            return snapshot

    async def snapshot(self, *, cached: bool = False) -> tuple[dict, bool | None]:
        with self.store.locked():
            state = self.state()
        if state["registry"] is None:
            return self.registry_request({"method": "members"}), True
        snapshot = state["snapshot"]
        reachable = None
        if not cached:
            try:
                refreshed = await network.request(
                    state["registry"],
                    {
                        "method": "members",
                        "cluster_id": snapshot["cluster_id"] if snapshot else None,
                    },
                    timeout=self.timeout,
                )
                return self.cache(state, refreshed), True
            except ClusterUnavailable:
                reachable = False
        if snapshot is None:
            raise ClusterUnavailable("no_cached_members")
        return snapshot, reachable

    async def status(self, *, cached: bool = False) -> dict:
        with self.store.locked():
            state = self.state()
        try:
            snapshot, reachable = await self.snapshot(cached=cached)
        except ClusterUnavailable:
            snapshot, reachable = None, None if cached else False
        return dict(
            cluster_id=snapshot["cluster_id"] if snapshot else None,
            name=snapshot["name"] if snapshot else None,
            member_id=state["member"]["id"],
            joined=bool(
                snapshot
                and any(
                    item["id"] == state["member"]["id"] for item in snapshot["members"]
                )
            ),
            registry=state["registry"],
            registry_reachable=reachable,
            stale=reachable is not True,
            fetched_at=snapshot["fetched_at"] if snapshot else None,
        )

    async def leave(self, member_id: str | None = None) -> dict:
        with self.store.locked():
            state = self.state()
        target = member_id or state["member"]["id"]
        request = dict(
            method="leave",
            member_id=target,
            cluster_id=state["snapshot"]["cluster_id"] if state["snapshot"] else None,
        )
        if state["registry"] is None:
            self.registry_request(request)
        else:
            snapshot = await network.request(
                state["registry"], request, timeout=self.timeout
            )
            self.cache(state, snapshot)
            if target == state["member"]["id"]:
                with self.store.locked():
                    if self.state()["member"] != state["member"]:
                        raise ClusterError("state_changed")
                    self.store.clear()
        return {"removed": target}
