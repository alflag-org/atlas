"""Durable membership transitions; publication always follows a successful write."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Coroutine
from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import Any, TypeVar

from atlas_core.cluster import ClusterError, ClusterMember, MemberState

from .model import merge_all, record
from .store import State, Store

T = TypeVar("T")


async def complete(operation: Coroutine[Any, Any, T]) -> T:
    """Settle an in-flight durable operation before propagating cancellation."""
    task = asyncio.create_task(operation)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Thread I/O cannot be cancelled. Keep ownership until publication too.
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


class Membership:
    def __init__(
        self, store: Store, state: State, local: ClusterMember, suspicion: float
    ) -> None:
        self.store = store
        self.state = state
        self.local = local
        self.suspicion = suspicion
        self.suspected: dict[str, float] = {}
        self.unconfirmed = set(state.members) - {local.id}
        self.updates: OrderedDict[str, ClusterMember] = OrderedDict()
        self.failed = False
        self.lock = asyncio.Lock()

    def own(
        self, incarnation: int, state: MemberState = MemberState.ALIVE
    ) -> ClusterMember:
        return record(asdict(replace(self.local, incarnation=incarnation, state=state)))

    async def commit(self, state: State) -> None:
        # The caller holds lock from reading the current state through publication.
        await complete(self._persist(state))

    async def _persist(self, state: State) -> None:
        if self.failed:
            raise ClusterError("persistence_failed")
        try:
            await asyncio.to_thread(self.store.write, state)
        except (OSError, ClusterError):
            # Block all subsequent publication until an operator repairs storage.
            self.failed = True
            raise ClusterError("persistence_failed") from None
        previous = self.state.members
        self.state = state
        for member in state.members.values():
            if previous.get(member.id) == member:
                continue
            self.updates[member.id] = member
            if member.state is MemberState.SUSPECT:
                self.suspected[member.id] = time.monotonic() + self.suspicion
            else:
                self.suspected.pop(member.id, None)

    async def restart(self) -> None:
        async with self.lock:
            if self.state.membership != "joined":
                return
            members = self.state.members.copy()
            members[self.local.id] = self.own(members[self.local.id].incarnation + 1)
            await self.commit(replace(self.state, members=members))
            for member in members.values():
                if member.state is MemberState.SUSPECT:
                    self.suspected[member.id] = time.monotonic() + self.suspicion

    async def apply(self, incoming: tuple[ClusterMember, ...]) -> None:
        async with self.lock:
            await self._apply(incoming)

    async def _apply(self, incoming: tuple[ClusterMember, ...]) -> None:
        if self.failed:
            raise ClusterError("persistence_failed")
        members = merge_all(self.state.members, incoming)
        own = members[self.local.id]
        membership = self.state.membership
        if membership == "left":
            # Only a local join may end persisted leave intent.
            members[self.local.id] = self.state.members[self.local.id]
        elif own.state in {MemberState.SUSPECT, MemberState.UNREACHABLE}:
            members[self.local.id] = self.own(own.incarnation + 1)
        elif own.state is MemberState.LEFT:
            membership = "left"
        if members != self.state.members or membership != self.state.membership:
            await self.commit(
                replace(self.state, members=members, membership=membership)
            )

    async def suspect(self, target: ClusterMember) -> None:
        async with self.lock:
            current = self.state.members.get(target.id)
            if current == target and target.state is MemberState.ALIVE:
                await self._apply((replace(target, state=MemberState.SUSPECT),))

    async def expire(self) -> None:
        async with self.lock:
            for member_id, deadline in tuple(self.suspected.items()):
                if time.monotonic() < deadline:
                    continue
                current = self.state.members[member_id]
                # commit() replaces/removes this deadline whenever the record changes.
                await self._apply((replace(current, state=MemberState.UNREACHABLE),))

    def snapshot(self) -> dict:
        members = []
        for member in sorted(self.state.members.values(), key=lambda item: item.id):
            raw = asdict(member)
            if member.id in self.unconfirmed and member.state is MemberState.ALIVE:
                raw["state"] = MemberState.UNKNOWN
            members.append(raw)
        return {"members": members, "fetched_at": timestamp(), "observation": "local"}
