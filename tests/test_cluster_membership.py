from __future__ import annotations

import asyncio
import copy
from dataclasses import asdict

import pytest

from atlas.cluster import network
from atlas.cluster.node import Node, local_member
from atlas_core.cluster import ClusterClient, ClusterError, ClusterUnavailable


def metadata(name):
    return dict(
        id=name,
        hostname=name,
        address=f"ops@{name}",
        role="app",
        site="lab",
        labels={"role": "app"},
    )


@pytest.fixture
def registry(tmp_path, monkeypatch):
    authority = Node(tmp_path / "registry")
    authority.initialize("homelab", metadata("registry"))

    async def request(address, payload, *, timeout):
        assert address == "ops@registry"
        return authority.registry_request(payload)

    monkeypatch.setattr(network, "request", request)
    return authority


def test_membership_api_and_offline_cache(registry, tmp_path, monkeypatch):
    async def scenario():
        first, second = [Node(tmp_path / name) for name in ("one", "two")]
        await first.join("ops@registry", metadata("one"))
        await second.join("ops@registry", metadata("two"))
        async with ClusterClient.connect(first.store.directory) as client:
            status = await client.status()
            assert status.joined and status.registry_reachable and not status.stale
            members = await client.members()
            assert [m.id for m in members] == ["one", "registry", "two"]
            assert asdict(next(iter(members))) == metadata("one")
            assert not members.stale
            cached = await client.members(cached=True)
            assert cached.stale and cached.members == members.members
            assert cached.fetched_at == members.fetched_at
            assert (await client.status(cached=True)).registry_reachable is None

            async def offline(*args, **kwargs):
                raise ClusterUnavailable("registry_unavailable")

            monkeypatch.setattr(network, "request", offline)
            stale = await client.members()
            assert stale.stale and stale.fetched_at == members.fetched_at
            status = await client.status()
            assert status.joined and status.stale and status.registry_reachable is False
            with pytest.raises(ClusterUnavailable):
                await first.leave()
            assert (await client.members(cached=True)).members == members.members
        assert (await registry.status())["registry_reachable"] is True
        assert (await registry.snapshot(cached=True))[1] is True
        with pytest.raises(ClusterError, match="not_joined"):
            await ClusterClient(tmp_path / "unjoined").members()

    asyncio.run(scenario())


def test_lost_join_response_is_retryable_and_leave_is_idempotent(
    registry, tmp_path, monkeypatch
):
    async def scenario():
        worker = Node(tmp_path / "worker")
        original = network.request

        async def lost(*args, **kwargs):
            await original(*args, **kwargs)
            raise ClusterUnavailable("registry_unavailable")

        monkeypatch.setattr(network, "request", lost)
        with pytest.raises(ClusterUnavailable):
            await worker.join("ops@registry", metadata("worker"))
        status = await worker.status()
        assert (
            not status["joined"]
            and status["cluster_id"] is None
            and status["registry_reachable"] is False
        )
        assert (await worker.status(cached=True))["registry_reachable"] is None
        with pytest.raises(ClusterUnavailable, match="no_cached_members"):
            await worker.snapshot(cached=True)
        revision = registry.registry_request({"method": "members"})["revision"]
        monkeypatch.setattr(network, "request", original)
        await worker.join("ops@registry", metadata("worker"))
        assert registry.registry_request({"method": "members"})["revision"] == revision
        with pytest.raises(ClusterError, match="already_joined"):
            await worker.join("ops@registry", metadata("worker"))
        with pytest.raises(ClusterError, match="already_joined"):
            registry.initialize("again", metadata("registry"))
        monkeypatch.setattr(network, "request", lost)
        with pytest.raises(ClusterUnavailable):
            await worker.leave()
        assert worker.store.path.exists()
        monkeypatch.setattr(network, "request", original)
        assert await worker.leave() == {"removed": "worker"}
        assert not worker.store.path.exists()
        with pytest.raises(ClusterError, match="cannot_remove_registry"):
            await registry.leave()

    asyncio.run(scenario())


def test_registry_conflicts_removal_and_metadata_refresh(registry, tmp_path):
    async def scenario():
        worker = Node(tmp_path / "worker")
        await worker.join("ops@registry", metadata("worker"))
        with pytest.raises(ClusterError, match="not_registry"):
            worker.registry_request({"method": "members"})
        with pytest.raises(ClusterError, match="member_conflict"):
            registry.registry_request(
                {
                    "method": "join",
                    "registration_id": "test-registration",
                    "member": metadata("worker") | {"role": "db"},
                }
            )
        await registry.leave("worker")
        status = await worker.status()
        assert not status["joined"] and not status["stale"]
        await worker.leave()
        await worker.join("ops@registry", metadata("worker") | {"role": "db"})
        assert (await worker.snapshot())[0]["members"][-1]["role"] == "db"
        await worker.leave("other")
        assert worker.store.path.exists()
        with pytest.raises(ClusterError, match="cluster_changed"):
            registry.registry_request({"method": "members", "cluster_id": "wrong"})
        with pytest.raises(ClusterError, match="unknown_method"):
            registry.registry_request({"method": "execute"})

    asyncio.run(scenario())


def test_pending_registration_is_not_promoted_by_an_unrelated_snapshot(
    registry, tmp_path, monkeypatch
):
    async def unavailable(*args, **kwargs):
        raise ClusterUnavailable("registry_unavailable")

    async def scenario():
        worker = Node(tmp_path / "worker")
        original = network.request
        monkeypatch.setattr(network, "request", unavailable)
        with pytest.raises(ClusterUnavailable):
            await worker.join("ops@registry", metadata("worker"))
        monkeypatch.setattr(network, "request", original)
        assert not (await worker.status())["joined"]
        assert worker.store.read()["snapshot"] is None
        await worker.join("ops@registry", metadata("worker"))
        assert (await worker.status())["joined"]

    asyncio.run(scenario())


def test_late_snapshot_cannot_replace_newer_state(registry, tmp_path, monkeypatch):
    async def scenario():
        worker = Node(tmp_path / "worker")
        await worker.join("ops@registry", metadata("worker"))
        original = worker.store.read()
        old = copy.deepcopy(original["snapshot"])
        registry.registry_request(
            {
                "method": "join",
                "registration_id": "test-registration",
                "member": metadata("new"),
            }
        )
        newer, _ = await worker.snapshot()
        with pytest.raises(ClusterError, match="registry_revision_regressed"):
            worker.cache(original, old)
        assert worker.store.read()["snapshot"] == newer
        with pytest.raises(ClusterError, match="cluster_changed"):
            worker.cache(original, old | {"cluster_id": "replacement"})
        with worker.store.locked():
            state = worker.state()
            state["registry"] = "ops@other"
            worker.store.write(state)
        with pytest.raises(ClusterError, match="state_changed"):
            worker.cache(original, newer)

    asyncio.run(scenario())


def test_invalid_remote_snapshots_do_not_replace_cache(registry, tmp_path, monkeypatch):
    async def scenario():
        worker = Node(tmp_path / "worker")
        original = network.request

        async def wrong_join(*args, **kwargs):
            return registry.registry_request({"method": "members"})

        monkeypatch.setattr(network, "request", wrong_join)
        with pytest.raises(ClusterError, match="invalid_snapshot"):
            await worker.join("ops@registry", metadata("worker"))
        monkeypatch.setattr(network, "request", original)
        await worker.join("ops@registry", metadata("worker"))
        saved = worker.store.path.read_bytes()
        good = worker.store.read()["snapshot"]
        bad = [
            None,
            {},
            good | {"revision": True},
            good | {"members": []},
            good | {"members": good["members"] * 2},
            good | {"fetched_at": "bad"},
            good | {"members": [{"id": "partial"}]},
            good | {"registrations": {}},
            good | {"registrations": list(good["registrations"])},
            good | {"registrations": dict.fromkeys(good["registrations"], "")},
        ]
        for snapshot in bad:

            async def response(*args, snapshot=snapshot, **kwargs):
                return snapshot

            monkeypatch.setattr(network, "request", response)
            with pytest.raises(ClusterError, match="invalid_snapshot"):
                await worker.snapshot()
            assert worker.store.path.read_bytes() == saved

    asyncio.run(scenario())


def test_host_profile_is_the_metadata_source(tmp_path, monkeypatch):
    host = tmp_path / "host.yml"
    host.write_text("version: 1\nhost:\n  id: host-1\n  role: app\n  site: home\n")
    monkeypatch.setenv("ATLAS_HOST_FILE", str(host))
    monkeypatch.setattr("socket.gethostname", lambda: "node")
    monkeypatch.setattr("socket.getfqdn", lambda: "node.example.test")
    assert local_member(None, {"env": "test"}) == dict(
        id="host-1",
        hostname="node",
        address="node.example.test",
        role="app",
        site="home",
        labels={"env": "test"},
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"id": ""},
        {"id": "x" * 513},
        {"id": "line\nfeed"},
        {"site": 4},
        {"address": "-oProxyCommand=bad"},
        {"labels": []},
        {"labels": {str(i): "x" for i in range(65)}},
        {"labels": {"": "x"}},
        {"labels": {"key": None}},
        {"unexpected": "field"},
    ],
)
def test_invalid_metadata_is_rejected_without_mutation(registry, changes):
    saved = registry.store.path.read_bytes()
    with pytest.raises(ClusterError):
        registry.registry_request(
            {
                "method": "join",
                "registration_id": "test-registration",
                "member": metadata("bad") | changes,
            }
        )
    assert registry.store.path.read_bytes() == saved


@pytest.mark.parametrize("rejoin_complete", [False, True])
def test_delayed_leave_cannot_clear_a_new_registration(
    registry, tmp_path, monkeypatch, rejoin_complete
):
    async def scenario():
        worker = Node(tmp_path / "worker")
        await worker.join("ops@registry", metadata("worker"))
        original = network.request
        both_started, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def delayed(address, payload, *, timeout):
            nonlocal calls
            response = await original(address, payload, timeout=timeout)
            calls += 1
            if calls == 2:
                both_started.set()
                await release.wait()
            else:
                await both_started.wait()
            return response

        monkeypatch.setattr(network, "request", delayed)
        first = asyncio.create_task(worker.leave())
        second = asyncio.create_task(worker.leave())
        await first
        monkeypatch.setattr(network, "request", original)
        if rejoin_complete:
            await worker.join("ops@registry", metadata("worker"))
        else:

            async def unavailable(*args, **kwargs):
                raise ClusterUnavailable("registry_unavailable")

            monkeypatch.setattr(network, "request", unavailable)
            with pytest.raises(ClusterUnavailable):
                await worker.join("ops@registry", metadata("worker"))
        registration = worker.store.read()["registration_id"]
        release.set()
        with pytest.raises(ClusterError, match="state_changed"):
            await second
        assert worker.store.read()["registration_id"] == registration
        monkeypatch.setattr(network, "request", original)
        if not rejoin_complete:
            await worker.join("ops@registry", metadata("worker"))
        assert (await worker.status())["joined"]

    asyncio.run(scenario())


def test_leave_requires_confirmation_from_the_same_registry(
    registry, tmp_path, monkeypatch
):
    async def scenario():
        worker = Node(tmp_path / "worker")
        await worker.join("ops@registry", metadata("worker"))
        snapshot = registry.registry_request({"method": "members"})
        for response, code in [
            (snapshot, "invalid_snapshot"),
            (snapshot | {"cluster_id": "other"}, "cluster_changed"),
        ]:

            async def request(*args, response=response, **kwargs):
                return response

            monkeypatch.setattr(network, "request", request)
            with pytest.raises(ClusterError, match=code):
                await worker.leave()
            assert worker.store.path.exists()

    asyncio.run(scenario())


def test_restored_registry_cannot_label_old_cache_as_fresh(registry, tmp_path):
    async def scenario():
        worker = Node(tmp_path / "worker")
        await worker.join("ops@registry", metadata("worker"))
        backup = registry.store.path.read_bytes()
        registry.registry_request(
            {
                "method": "join",
                "registration_id": "test-registration",
                "member": metadata("new"),
            }
        )
        client = ClusterClient(worker.store.directory)
        latest = await client.members()
        registry.store.path.write_bytes(backup)
        with pytest.raises(ClusterError, match="registry_revision_regressed"):
            await client.members()
        saved = await client.members(cached=True)
        assert saved.stale and saved.members == latest.members

    asyncio.run(scenario())


def test_delayed_leave_request_cannot_remove_a_new_registration(
    registry, tmp_path, monkeypatch
):
    async def scenario():
        worker = Node(tmp_path / "worker")
        await worker.join("ops@registry", metadata("worker"))
        original = network.request
        both_started, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def delayed(address, payload, *, timeout):
            nonlocal calls
            calls += 1
            if calls == 2:
                both_started.set()
                await release.wait()
            else:
                await both_started.wait()
            # Delay delivery, not the response: the registry has not seen this leave.
            return await original(address, payload, timeout=timeout)

        monkeypatch.setattr(network, "request", delayed)
        first = asyncio.create_task(worker.leave())
        second = asyncio.create_task(worker.leave())
        await first
        monkeypatch.setattr(network, "request", original)
        await worker.join("ops@registry", metadata("worker"))
        new_registration = worker.store.read()["registration_id"]
        release.set()
        with pytest.raises(ClusterError, match="registration_changed"):
            await second
        assert (await worker.status())["joined"]
        assert (
            registry.registry_request({"method": "members"})["registrations"]["worker"]
            == new_registration
        )

    asyncio.run(scenario())


def test_remote_administrator_removes_only_the_observed_generation(registry, tmp_path):
    async def scenario():
        worker, observer = Node(tmp_path / "worker"), Node(tmp_path / "observer")
        await worker.join("ops@registry", metadata("worker"))
        await observer.join("ops@registry", metadata("observer"))
        await worker.leave()
        await worker.join("ops@registry", metadata("worker"))
        with pytest.raises(ClusterError, match="registration_changed"):
            await observer.leave("worker")
        assert (await worker.status())["joined"]
        await observer.snapshot()
        assert await observer.leave("worker") == {"removed": "worker"}
        assert not (await worker.status())["joined"]

    asyncio.run(scenario())


@pytest.mark.parametrize("committed", [False, True])
def test_abort_discards_only_pending_local_state(
    registry, tmp_path, monkeypatch, committed
):
    async def scenario():
        worker = Node(tmp_path / "worker")
        original = network.request
        attempts = 0

        async def unavailable(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if committed:
                await original(*args, **kwargs)
            raise ClusterUnavailable("registry_unavailable")

        monkeypatch.setattr(network, "request", unavailable)
        with pytest.raises(ClusterUnavailable):
            await worker.join("ops@registry", metadata("worker"))
        pending = worker.store.read()
        result = worker.abort_join()
        assert result["registry"] == "ops@registry"
        assert result["discarded"] == "worker"
        assert result["registration_id"] == pending["registration_id"]
        assert "inspect the registry" in result["warning"]
        assert not worker.store.path.exists() and attempts == 1
        monkeypatch.setattr(network, "request", original)
        current = registry.registry_request({"method": "members"})
        assert ("worker" in current["registrations"]) == committed
        if committed:
            # Aborting must neither remove the remote entry nor silently adopt it.
            with pytest.raises(ClusterError, match="member_conflict"):
                await worker.join("ops@registry", metadata("worker"))
            assert not (await worker.status())["joined"]
            assert worker.store.read()["snapshot"] is None
            await registry.leave("worker")
            worker.abort_join()
        await worker.join("ops@registry", metadata("worker"))
        for node in (worker, registry):
            saved = node.store.path.read_bytes()
            with pytest.raises(ClusterError, match="not_pending_join"):
                node.abort_join()
            assert node.store.path.read_bytes() == saved

    asyncio.run(scenario())


def test_aborted_join_response_cannot_replace_corrected_destination(
    registry, tmp_path, monkeypatch
):
    async def scenario():
        worker, other = Node(tmp_path / "worker"), Node(tmp_path / "other")
        other.initialize("other", metadata("other"))
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed(address, payload, *, timeout):
            if address == "ops@wrong":
                result = registry.registry_request(payload)
                started.set()
                await release.wait()
                return result
            return other.registry_request(payload)

        monkeypatch.setattr(network, "request", delayed)
        old = asyncio.create_task(worker.join("ops@wrong", metadata("worker")))
        await started.wait()
        worker.abort_join()
        await worker.join("ops@correct", metadata("worker"))
        saved = worker.store.path.read_bytes()
        release.set()
        with pytest.raises(ClusterError, match="state_changed"):
            await old
        assert worker.store.path.read_bytes() == saved
        assert (await worker.status())["name"] == "other"

    asyncio.run(scenario())


def test_registry_rejects_unconditional_removal(registry, tmp_path):
    async def scenario():
        worker = Node(tmp_path / "worker")
        await worker.join("ops@registry", metadata("worker"))
        saved = registry.store.path.read_bytes()
        with pytest.raises(ClusterError, match="registration_required"):
            registry.registry_request({"method": "leave", "member_id": "worker"})
        assert registry.store.path.read_bytes() == saved
        with pytest.raises(ClusterError, match="registration_changed"):
            registry.registry_request(
                {"method": "leave", "member_id": "worker", "registration_id": None}
            )
        assert registry.store.path.read_bytes() == saved

    asyncio.run(scenario())


def test_join_response_must_confirm_the_pending_generation(
    registry, tmp_path, monkeypatch
):
    async def wrong_generation(address, payload, *, timeout):
        return registry.registry_request(
            payload | {"registration_id": "different-registration"}
        )

    async def scenario():
        worker = Node(tmp_path / "worker")
        monkeypatch.setattr(network, "request", wrong_generation)
        with pytest.raises(ClusterError, match="invalid_snapshot"):
            await worker.join("ops@registry", metadata("worker"))
        assert worker.store.read()["snapshot"] is None
        worker.abort_join()

    asyncio.run(scenario())


@pytest.mark.parametrize("registered", [False, True])
def test_delayed_join_cannot_restore_a_successfully_left_generation(
    registry, tmp_path, monkeypatch, registered
):
    async def scenario():
        worker = Node(tmp_path / "worker")
        original = network.request
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed(address, payload, *, timeout):
            if payload["method"] == "join" and not started.is_set():
                started.set()
                await release.wait()
            return await original(address, payload, timeout=timeout)

        monkeypatch.setattr(network, "request", delayed)
        first = asyncio.create_task(worker.join("ops@registry", metadata("worker")))
        await started.wait()
        if registered:
            # A retry confirms the same generation while the first request is delayed.
            await worker.join("ops@registry", metadata("worker"))
        assert await worker.leave() == {"removed": "worker"}
        release.set()
        with pytest.raises(ClusterError, match="registration_retired"):
            await first
        assert not worker.store.path.exists()
        assert registry.registry_request({"method": "members"})["members"] == [
            metadata("registry")
        ]
        await worker.join("ops@registry", metadata("worker"))
        assert (await worker.status())["joined"]

    asyncio.run(scenario())
