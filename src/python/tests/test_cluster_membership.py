"""Merge laws, ownership, bounded metadata, durable refutation and leave intent."""

import itertools
from dataclasses import asdict, replace

import pytest

from atlas.cluster.membership import Membership
from atlas.cluster.model import MAX_MEMBERS, endpoint, merge, merge_all, record, records
from atlas.cluster.store import State, Store
from atlas_core.cluster import ClusterError, ClusterMember, MemberState, PeerEndpoint


def member(name="a", incarnation=1, state=MemberState.ALIVE):
    return ClusterMember(
        name,
        f"{name}.example.test",
        PeerEndpoint("127.0.0.1", 7947),
        "app",
        "lab",
        {},
        incarnation,
        state,
    )


def test_merge_laws_and_metadata_conflict():
    variants = [
        replace(member(), state=state)
        for state in (
            MemberState.ALIVE,
            MemberState.SUSPECT,
            MemberState.UNREACHABLE,
            MemberState.LEFT,
        )
    ]
    variants += [member(incarnation=2)]
    for sequence in itertools.permutations(variants):
        result = None
        for item in sequence:
            result = merge(result, item)
            assert merge(result, item) == result
        assert result == member(incarnation=2)
    for left, right in itertools.product(variants, repeat=2):
        assert merge(left, right) == merge(right, left)
    with pytest.raises(ClusterError, match="member_conflict"):
        merge(member(), replace(member(), labels={"different": "metadata"}))


def test_limit_counts_tombstones_and_rejects_entire_snapshot():
    existing = {
        str(i): member(str(i), state=MemberState.LEFT) for i in range(MAX_MEMBERS)
    }
    with pytest.raises(ClusterError, match="membership_too_large"):
        merge_all(existing, (member("new"),))
    assert len(existing) == MAX_MEMBERS
    with pytest.raises(ClusterError, match="duplicate_member"):
        records([asdict(member()), asdict(member())])
    for raw in ({}, [asdict(member())] * 257):
        with pytest.raises(ClusterError, match="membership_too_large"):
            records(raw)
    assert records([asdict(member())]) == (member(),)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("atlas.example.test", PeerEndpoint("atlas.example.test", 7947)),
        ("192.0.2.1:1234", PeerEndpoint("192.0.2.1", 1234)),
        ("[2001:db8::1]:1234", PeerEndpoint("2001:db8::1", 1234)),
        ("2001:db8::1", PeerEndpoint("2001:db8::1", 7947)),
        ("[::1]", PeerEndpoint("::1", 7947)),
    ],
)
def test_address_parser(value, expected):
    assert endpoint(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "user@host",
        "ssh://host",
        "a:0",
        "a:65536",
        "a:port",
        "[::1",
        "[host]",
        "0.0.0.0",
        "::",
        "224.0.0.1",
        "[fe80::1%eth0]",
        "-alias",
        "a..b",
        "",
        12,
    ],
)
def test_invalid_addresses(value):
    with pytest.raises(ClusterError):
        endpoint(value)


@pytest.mark.parametrize(
    "change",
    [
        {"incarnation": True},
        {"incarnation": -1},
        {"incarnation": 2**63},
        {"labels": []},
        {"labels": {str(i): "v" for i in range(17)}},
        {"state": "unknown"},
        {"state": []},
        {"id": "\n"},
        {"hostname": "a" * 254},
        {"endpoint": {}},
        {"endpoint": {"host": "a", "port": True}},
        {"labels": {str(i): "x" * 128 for i in range(6)}},
        {"unexpected": 1},
    ],
)
def test_invalid_records(change):
    with pytest.raises(ClusterError):
        record(asdict(member()) | change)


def test_refutation_deadline_restart_and_leave(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr("atlas.cluster.membership.time.monotonic", lambda: now[0])
    a, b = member(), member("b")
    store = Store(tmp_path)
    state = State("a", "cluster", "lab", "joined", {"a": a, "b": b})
    membership = Membership(store, state, a, 10)
    assert membership.snapshot()["members"][1]["state"] == "unknown"
    membership.suspect(b)
    deadline = membership.suspected["b"]
    membership.apply((replace(b, state=MemberState.SUSPECT),))
    assert membership.suspected["b"] == deadline
    membership.expire()
    assert membership.state.members["b"].state is MemberState.SUSPECT
    now[0] = 111
    membership.expire()
    assert membership.state.members["b"].state is MemberState.UNREACHABLE
    membership.apply((b,))
    assert membership.state.members["b"].state is MemberState.UNREACHABLE
    membership.apply((replace(a, state=MemberState.SUSPECT),))
    assert membership.state.members["a"] == member(incarnation=2)
    assert '"incarnation":2' in store.path.read_text()
    membership.apply((replace(member(incarnation=2), state=MemberState.LEFT),))
    assert membership.state.membership == "left"
    membership.restart()
    membership.apply((member(incarnation=3),))
    assert membership.state.members["a"].state is MemberState.LEFT
    assert membership.state.members["a"].incarnation == 2


def test_persistence_failure_stops_publication(tmp_path, monkeypatch):
    a = member()
    membership = Membership(
        Store(tmp_path), State("a", members={"a": a}, membership="joined"), a, 10
    )

    def fail(_state):
        raise OSError("secret storage body")

    monkeypatch.setattr(membership.store, "write", fail)
    for _ in range(2):
        with pytest.raises(ClusterError, match=r"^persistence_failed$"):
            membership.apply((replace(a, state=MemberState.SUSPECT),))
    with pytest.raises(ClusterError, match="persistence_failed"):
        membership.commit(membership.state)
    assert membership.state.members["a"] == a
    assert not membership.updates


def test_restart_preserves_suspicion_and_updates_local_metadata(tmp_path, monkeypatch):
    state = State(
        "a",
        members={"a": member(), "b": member("b", state=MemberState.SUSPECT)},
        membership="joined",
    )
    local = replace(member(), labels={"revision": "new"})
    membership = Membership(Store(tmp_path), state, local, 10)
    monkeypatch.setattr("atlas.cluster.membership.time.monotonic", lambda: 100)
    membership.restart()
    assert membership.suspected["b"] == 110
    assert membership.state.members["a"].incarnation == 2
    assert membership.state.members["a"].labels == {"revision": "new"}
    assert membership.snapshot()["members"][1]["state"] == "suspect"


def test_metadata_reserves_space_for_future_incarnation_and_state():
    from atlas.cluster.model import MAX_INCARNATION, MAX_RECORD_BYTES, encoded
    from atlas.cluster.wire import MAX_DATAGRAM

    from .test_cluster_transport import cipher

    largest = replace(
        member(incarnation=MAX_INCARNATION, state=MemberState.UNREACHABLE),
        labels={"one": "x" * 128, "two": "y" * 128, "three": "w" * 128},
    )
    spare = MAX_RECORD_BYTES - len(encoded(asdict(largest))) - len(',"extra":""')
    largest = replace(largest, labels=largest.labels | {"extra": "z" * spare})
    assert len(encoded(asdict(largest))) == MAX_RECORD_BYTES
    assert record(asdict(largest)) == largest
    assert (
        len(cipher().seal("gossip", {"member": asdict(largest)}, limit=MAX_DATAGRAM))
        <= MAX_DATAGRAM
    )
    almost = replace(
        largest,
        incarnation=1,
        state=MemberState.ALIVE,
        labels=largest.labels | {"extra": "z" * (spare + 1)},
    )
    with pytest.raises(ClusterError, match="member_too_large"):
        record(asdict(almost))
