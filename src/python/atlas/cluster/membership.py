"""Durable membership transitions; publication always follows a successful write."""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import asdict, replace
from datetime import UTC, datetime

from atlas_core.cluster import ClusterError, ClusterMember, MemberState

from .model import merge_all, record
from .store import State, Store


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

    def own(
        self, incarnation: int, state: MemberState = MemberState.ALIVE
    ) -> ClusterMember:
        return record(asdict(replace(self.local, incarnation=incarnation, state=state)))

    def commit(self, state: State) -> None:
        if self.failed:
            raise ClusterError("persistence_failed")
        try:
            self.store.write(state)
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

    def restart(self) -> None:
        if self.state.membership != "joined":
            return
        members = self.state.members.copy()
        members[self.local.id] = self.own(members[self.local.id].incarnation + 1)
        self.commit(replace(self.state, members=members))
        for member in members.values():
            if member.state is MemberState.SUSPECT:
                self.suspected[member.id] = time.monotonic() + self.suspicion

    def apply(self, incoming: tuple[ClusterMember, ...]) -> None:
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
            self.commit(replace(self.state, members=members, membership=membership))

    def suspect(self, target: ClusterMember) -> None:
        current = self.state.members.get(target.id)
        if current == target and target.state is MemberState.ALIVE:
            self.apply((replace(target, state=MemberState.SUSPECT),))

    def expire(self) -> None:
        for member_id, deadline in tuple(self.suspected.items()):
            if time.monotonic() < deadline:
                continue
            current = self.state.members[member_id]
            # commit() replaces/removes this deadline whenever the record changes.
            self.apply((replace(current, state=MemberState.UNREACHABLE),))

    def snapshot(self) -> dict:
        members = []
        for member in sorted(self.state.members.values(), key=lambda item: item.id):
            raw = asdict(member)
            if member.id in self.unconfirmed and member.state is MemberState.ALIVE:
                raw["state"] = MemberState.UNKNOWN
            members.append(raw)
        return {"members": members, "fetched_at": timestamp(), "observation": "local"}
