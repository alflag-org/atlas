"""Cluster control operations and the independent probe, gossip and sync loops."""

from __future__ import annotations

import asyncio
import random
import socket
from dataclasses import asdict, dataclass, replace
from uuid import uuid4

from atlas_core.cluster import ClusterError, ClusterMember, MemberState, PeerEndpoint

from .config import Config
from .membership import Membership, timestamp
from .model import endpoint, merge_all, parse_endpoint, record, records, text
from .network import exchange, resolve
from .store import Credential, State
from .wire import MAX_DATAGRAM, Cipher


@dataclass(frozen=True)
class Timers:
    probe_interval: float = 2
    probe_timeout: float = 1
    suspicion: float = 10
    gossip_interval: float = 0.5
    sync_interval: float = 10
    exchange_timeout: float = 3
    join_timeout: float = 10


DEFAULT_TIMERS = Timers()


@dataclass
class PendingProbe:
    target_id: str
    incarnation: int
    event: asyncio.Event


class Node:
    def __init__(
        self, config: Config, membership: Membership, timers: Timers = DEFAULT_TIMERS
    ) -> None:
        self.config = config
        self.membership = membership
        self.timers = timers
        self.cipher: Cipher | None = None
        self.last_contact: str | None = None
        self.pending: dict[str, PendingProbe] = {}
        self.control_lock = asyncio.Lock()
        self.transport = None
        self.family = 0
        self.relays: set[asyncio.Task] = set()
        self.sync_cursor = 0

    @property
    def state(self) -> State:
        return self.membership.state

    def credentials(self) -> Cipher:
        credential = Credential.read(self.config.credentials_file)
        if (
            self.state.cluster_id is not None
            and credential.cluster_id != self.state.cluster_id
        ):
            raise ClusterError("cluster_mismatch")
        if (
            self.state.credential_fingerprint is not None
            and credential.fingerprint != self.state.credential_fingerprint
        ):
            raise ClusterError("credential_mismatch")
        return Cipher(credential)

    def peers(self) -> list[ClusterMember]:
        return [
            item
            for item in self.state.members.values()
            if item.id != self.state.local_id and item.state is not MemberState.LEFT
        ]

    def candidates(self) -> list[PeerEndpoint]:
        # Include unreachable peers and DNS names, even when every seed is down.
        return list(
            dict.fromkeys(
                [
                    *self.state.seeds,
                    *self.config.seeds,
                    *(item.endpoint for item in self.peers()),
                ]
            )
        )

    def wire_snapshot(self) -> dict:
        return {
            "name": self.state.name,
            "members": [asdict(item) for item in self.state.members.values()],
        }

    def receive_snapshot(self, raw: dict) -> tuple[str, tuple[ClusterMember, ...]]:
        if set(raw) != {"name", "members"}:
            raise ClusterError("invalid_snapshot")
        name = text(raw["name"])
        if self.state.name is not None and name != self.state.name:
            raise ClusterError("cluster_name_conflict")
        return name, records(raw["members"])

    async def remote(self, method: str, data: dict) -> dict:
        if self.membership.failed:
            raise ClusterError("persistence_failed")
        if self.state.membership == "uninitialized":
            raise ClusterError("not_joined")
        if method == "sync":
            _, members = self.receive_snapshot(data)
            self.membership.apply(members)
        elif method != "fetch" or data or self.state.membership != "joined":
            raise ClusterError("invalid_request")
        self.last_contact = timestamp()
        return self.wire_snapshot()

    async def control(self, method: str, params: dict, _peer=None) -> dict:
        async with self.control_lock:
            if self.membership.failed:
                raise ClusterError("persistence_failed")
            if method == "status" and not params:
                return {
                    "local_id": self.state.local_id,
                    "cluster_id": self.state.cluster_id,
                    "name": self.state.name,
                    "membership": self.state.membership,
                    "last_peer_contact": self.last_contact,
                }
            if method == "members" and not params:
                if self.state.membership == "uninitialized":
                    raise ClusterError("not_joined")
                return self.membership.snapshot()
            if method == "init" and set(params) == {"name"}:
                return self.initialize(text(params["name"]))
            if method == "join" and set(params) == {"seeds"}:
                seeds = params["seeds"]
                if not isinstance(seeds, list) or not 1 <= len(seeds) <= 32:
                    raise ClusterError("invalid_seeds")
                return await self.join(tuple(endpoint(seed) for seed in seeds))
            if method == "leave" and not params:
                return await self.leave()
            raise ClusterError("invalid_request")

    def initialize(self, name: str) -> dict:
        if self.state.membership != "uninitialized":
            raise ClusterError("already_initialized")
        credential = Credential.create(self.config.credentials_file)
        own = self.membership.own(1)
        self.membership.commit(
            State(
                own.id,
                credential.cluster_id,
                name,
                "joined",
                {own.id: own},
                credential_fingerprint=credential.fingerprint,
            )
        )
        self.cipher = Cipher(credential)
        return {"cluster_id": credential.cluster_id, "membership": "joined"}

    async def join(self, seeds: tuple[PeerEndpoint, ...]) -> dict:
        cipher = self.credentials()
        last_error = "peer_unavailable"
        try:
            async with asyncio.timeout(self.timers.join_timeout):
                for seed in seeds:
                    try:
                        raw = await exchange(
                            seed,
                            cipher,
                            "fetch",
                            {},
                            timeout=self.timers.exchange_timeout,
                        )
                        name, remote = self.receive_snapshot(raw)
                        members = merge_all(self.state.members, remote)
                        previous = members.get(self.state.local_id)
                        own = self.membership.own(
                            1 if previous is None else previous.incarnation + 1
                        )
                        members = merge_all(members, (own,))
                        # Persist before publishing; a failed reply can leave a joined node.
                        self.membership.commit(
                            State(
                                own.id,
                                cipher.cluster_id,
                                name,
                                "joined",
                                members,
                                seeds,
                                cipher.fingerprint,
                            )
                        )
                        self.cipher = cipher
                        reply = await exchange(
                            seed,
                            cipher,
                            "sync",
                            self.wire_snapshot(),
                            timeout=self.timers.exchange_timeout,
                        )
                        _, incoming = self.receive_snapshot(reply)
                        self.membership.apply(incoming)
                        self.last_contact = timestamp()
                        return {
                            "membership": "joined",
                            "cluster_id": cipher.cluster_id,
                            "exchange_completed": True,
                            "scope": "local_and_seed",
                        }
                    except ClusterError as exc:
                        if self.membership.failed:
                            raise
                        last_error = exc.code
        except TimeoutError:
            last_error = "join_timeout"
        raise ClusterError(last_error)

    async def leave(self) -> dict:
        if self.state.membership == "uninitialized":
            raise ClusterError("not_joined")
        if self.state.membership != "left":
            members = self.state.members.copy()
            current = members[self.state.local_id]
            members[current.id] = self.membership.own(
                current.incarnation + 1, MemberState.LEFT
            )
            self.membership.commit(
                replace(self.state, membership="left", members=members)
            )
        propagated = False
        try:
            async with asyncio.timeout(self.timers.exchange_timeout):
                for peer in self.candidates():
                    if await self.synchronize(peer):
                        propagated = True
        except TimeoutError:
            pass  # Leave is durable locally even when no peer can acknowledge it.
        return {"membership": "left", "propagated_to_peer": propagated}

    async def synchronize(self, peer: PeerEndpoint) -> bool:
        if self.cipher is None or self.membership.failed:
            return False
        try:
            reply = await exchange(
                peer,
                self.cipher,
                "sync",
                self.wire_snapshot(),
                timeout=self.timers.exchange_timeout,
            )
            _, incoming = self.receive_snapshot(reply)
            self.membership.apply(incoming)
            self.last_contact = timestamp()
            return True
        except ClusterError:
            if self.membership.failed:
                raise
            return False

    async def send(self, peer: PeerEndpoint, kind: str, payload: dict) -> None:
        if self.cipher is None or self.membership.failed:
            return
        packet = self.cipher.seal(kind, payload, limit=MAX_DATAGRAM)
        try:
            async with asyncio.timeout(self.timers.probe_timeout):
                addresses = await resolve(peer, socket.SOCK_DGRAM, self.family)
                for _, _, _, _, address in addresses:
                    self.transport.sendto(packet, address)
        except (OSError, TimeoutError):
            pass  # DNS/send failure is a missed probe, never a membership deletion.

    async def probe(self, target: ClusterMember, *, indirect: bool = True) -> bool:
        request_id = uuid4().hex
        pending = PendingProbe(target.id, target.incarnation, asyncio.Event())
        self.pending[request_id] = pending
        payload = {
            "id": request_id,
            "target": target.id,
            "incarnation": target.incarnation,
            "reply": asdict(self.config.advertise),
        }
        try:
            await self.send(target.endpoint, "ping", payload)
            try:
                await asyncio.wait_for(pending.event.wait(), self.timers.probe_timeout)
            except TimeoutError:
                if not indirect:
                    return False
                helpers = [
                    peer
                    for peer in self.peers()
                    if peer.id != target.id and peer.state is MemberState.ALIVE
                ]
                for helper in random.sample(helpers, min(3, len(helpers))):
                    await self.send(
                        helper.endpoint,
                        "ping_req",
                        payload,
                    )
                try:
                    await asyncio.wait_for(
                        pending.event.wait(), self.timers.probe_timeout
                    )
                except TimeoutError:
                    return False
            # A concurrent sync can replace the record while this probe is waiting.
            # Its older ack must not confirm the replacement incarnation.
            if self.state.members[target.id].incarnation != target.incarnation:
                return False
            self.last_contact = timestamp()
            self.membership.unconfirmed.discard(target.id)
            return True
        finally:
            del self.pending[request_id]

    async def relay(self, payload: dict) -> None:
        target = self.state.members.get(payload["target"])
        if target is None or target.incarnation != payload["incarnation"]:
            return
        if await self.probe(target, indirect=False):
            await self.send(
                parse_endpoint(payload["reply"]),
                "ack",
                {
                    "id": payload["id"],
                    "target": target.id,
                    "incarnation": target.incarnation,
                },
            )

    async def datagrams(self, inbox, group: asyncio.TaskGroup) -> None:
        while True:
            packet, _ = await inbox.queue.get()
            if (
                self.cipher is None
                or self.membership.failed
                or self.state.membership != "joined"
            ):
                continue
            try:
                kind, payload = self.cipher.open(packet, limit=MAX_DATAGRAM)
                if kind == "gossip":
                    if set(payload) != {"member"}:
                        raise ClusterError("invalid_request")
                    self.membership.apply((record(payload["member"]),))
                    continue
                if (
                    kind not in {"ping", "ack", "ping_req"}
                    or not isinstance(payload.get("id"), str)
                    or len(payload["id"]) != 32
                    or not isinstance(payload.get("target"), str)
                    or type(payload.get("incarnation")) is not int
                ):
                    raise ClusterError("invalid_probe")
                if kind == "ack":
                    pending = self.pending.get(payload["id"])
                    if pending is not None and (
                        pending.target_id,
                        pending.incarnation,
                    ) == (payload["target"], payload["incarnation"]):
                        pending.event.set()
                else:
                    reply = parse_endpoint(payload.get("reply"))
                    if kind == "ping":
                        own = self.state.members[self.state.local_id]
                        if (
                            payload["target"] == own.id
                            and payload["incarnation"] == own.incarnation
                        ):
                            await self.send(
                                reply,
                                "ack",
                                {
                                    "id": payload["id"],
                                    "target": own.id,
                                    "incarnation": own.incarnation,
                                },
                            )
                    elif len(self.relays) < 16:
                        task = group.create_task(self.relay(payload))
                        self.relays.add(task)
                        task.add_done_callback(self.relays.discard)
            except ClusterError:
                if self.membership.failed:
                    raise
                # Untrusted input cannot terminate a required task or expose payloads.

    async def probes(self) -> None:
        while True:
            await asyncio.sleep(self.timers.probe_interval)
            if self.state.membership != "joined":
                continue
            self.membership.expire()
            peers = self.peers()
            if peers:
                target = random.choice(peers)
                if not await self.probe(target):
                    self.membership.suspect(target)

    async def gossip(self) -> None:
        while True:
            await asyncio.sleep(self.timers.gossip_interval)
            if self.state.membership != "joined" or not self.membership.updates:
                continue
            _, member = self.membership.updates.popitem(last=False)
            peers = self.peers()
            for peer in random.sample(peers, min(3, len(peers))):
                await self.send(peer.endpoint, "gossip", {"member": asdict(member)})

    async def sync(self) -> None:
        while True:
            await asyncio.sleep(self.timers.sync_interval)
            if self.state.membership != "joined":
                continue
            peers = self.candidates()
            if peers:
                peer = peers[self.sync_cursor % len(peers)]
                self.sync_cursor += 1
                await self.synchronize(peer)
