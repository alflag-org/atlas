"""Validated wire records and order-independent membership merge rules."""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import asdict, replace

from atlas_core.cluster import ClusterError, ClusterMember, MemberState, PeerEndpoint

MAX_MEMBERS = 256
MAX_RECORD_BYTES = 700
MAX_INCARNATION = (1 << 63) - 1
STATE_ORDER = {
    state: index
    for index, state in enumerate(
        (
            MemberState.ALIVE,
            MemberState.SUSPECT,
            MemberState.UNREACHABLE,
            MemberState.LEFT,
        )
    )
}


def encoded(value: object) -> bytes:
    return json.dumps(
        value, separators=(",", ":"), sort_keys=True, allow_nan=False
    ).encode()


def text(value: object, limit: int = 128, *, empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or len(value.encode()) > limit
        or (not value and not empty)
        or any(ord(c) < 32 for c in value)
    ):
        raise ClusterError("invalid_text")
    return value


def endpoint(value: object, *, bind: bool = False) -> PeerEndpoint:
    value = text(value, 300)
    host, port = value, 7947
    if value.startswith("["):
        match = re.fullmatch(r"\[([^\]]+)\](?::([0-9]+))?", value)
        if match is None:
            raise ClusterError("invalid_endpoint")
        host = match[1]
        port = int(match[2]) if match[2] else port
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            raise ClusterError("invalid_endpoint") from None
    elif value.count(":") == 1:
        host, port_text = value.split(":")
        if not port_text.isascii() or not port_text.isdigit():
            raise ClusterError("invalid_endpoint")
        port = int(port_text)
    if not 1 <= port <= 65535 or "%" in host:
        raise ClusterError("invalid_endpoint")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if (
            bind
            or len(host) > 253
            or not all(
                re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", part)
                for part in host.rstrip(".").split(".")
            )
        ):
            raise ClusterError("invalid_endpoint") from None
        host = host.lower()
    else:
        if not bind and (address.is_unspecified or address.is_multicast):
            raise ClusterError("invalid_endpoint")
        host = str(address)
    return PeerEndpoint(host, port)


def endpoint_text(peer: PeerEndpoint) -> str:
    host = f"[{peer.host}]" if ":" in peer.host else peer.host
    return f"{host}:{peer.port}"


def parse_endpoint(raw: object) -> PeerEndpoint:
    if not isinstance(raw, dict) or set(raw) != {"host", "port"}:
        raise ClusterError("invalid_endpoint")
    if type(raw["port"]) is not int:
        raise ClusterError("invalid_endpoint")
    return endpoint(endpoint_text(PeerEndpoint(text(raw["host"], 253), raw["port"])))


def record(raw: object) -> ClusterMember:
    if not isinstance(raw, dict) or set(raw) != set(ClusterMember.__dataclass_fields__):
        raise ClusterError("invalid_member")
    incarnation = raw["incarnation"]
    if type(incarnation) is not int or not 0 <= incarnation <= MAX_INCARNATION:
        raise ClusterError("invalid_incarnation")
    labels = raw["labels"]
    if not isinstance(labels, dict) or len(labels) > 16:
        raise ClusterError("invalid_labels")
    labels = {
        text(key, 64): text(value, 128, empty=True) for key, value in labels.items()
    }
    try:
        state = MemberState(raw["state"])
    except (ValueError, TypeError):
        raise ClusterError("invalid_member_state") from None
    if state not in STATE_ORDER:
        raise ClusterError("invalid_member_state")
    member = ClusterMember(
        text(raw["id"]),
        text(raw["hostname"], 253),
        parse_endpoint(raw["endpoint"]),
        text(raw["role"], empty=True),
        text(raw["site"], empty=True),
        labels,
        incarnation,
        state,
    )
    # This bound reserves room for the authenticated UDP envelope and gossip header.
    largest = replace(
        member, incarnation=MAX_INCARNATION, state=MemberState.UNREACHABLE
    )
    if len(encoded(asdict(largest))) > MAX_RECORD_BYTES:
        raise ClusterError("member_too_large")
    return member


def records(raw: object) -> tuple[ClusterMember, ...]:
    if not isinstance(raw, list) or len(raw) > MAX_MEMBERS:
        raise ClusterError("membership_too_large")
    result = tuple(record(item) for item in raw)
    if len({item.id for item in result}) != len(result):
        raise ClusterError("duplicate_member")
    return result


def merge(current: ClusterMember | None, incoming: ClusterMember) -> ClusterMember:
    if current is None or incoming.incarnation > current.incarnation:
        return incoming
    if incoming.incarnation < current.incarnation:
        return current
    if replace(current, state=incoming.state) != incoming:
        raise ClusterError("member_conflict")
    return (
        incoming
        if STATE_ORDER[incoming.state] > STATE_ORDER[current.state]
        else current
    )


def merge_all(
    current: dict[str, ClusterMember], incoming: tuple[ClusterMember, ...]
) -> dict[str, ClusterMember]:
    result = current.copy()
    for item in incoming:
        result[item.id] = merge(result.get(item.id), item)
    if len(result) > MAX_MEMBERS:
        raise ClusterError("membership_too_large")
    return result
