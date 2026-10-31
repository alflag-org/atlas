from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest

from atlas.cluster.workflow import digest
from atlas_core.ipc import IPCError

from .support import configure_environment, write_native_command
from .test_cluster_membership import node


async def completed(authority, operation_id):
    async with asyncio.timeout(15):
        while True:
            result = authority.workflows.operation(operation_id)
            if result["state"] not in {"prepared", "running"}:
                return result
            await asyncio.sleep(0.01)


def workflow(targets, **overrides):
    return {
        "schema": "atlas.workflow/v1",
        "name": "deploy",
        "steps": [
            {
                "id": "check",
                "targets": {"nodes": targets},
                "command": "sample",
                "mutates": False,
                "max_parallel": 3,
            },
            {
                "id": "deploy",
                "needs": ["check"],
                "targets": {"nodes": targets},
                "command": "sample",
                "arguments": ["literal $arg", "%n"],
                "mutates": True,
                "rollout": {"max_parallel": 2, "max_unavailable": 1},
                "verify": {"command": "sample", "timeout_seconds": 10},
                **overrides,
            },
        ],
    }


def test_three_node_workflow_uses_existing_executor_and_deduplicates(
    tmp_path, monkeypatch
):
    root = tmp_path / "program"
    write_native_command(root, "sample")
    _, _, var = configure_environment(
        monkeypatch,
        tmp_path,
        f"programs:\n  sample:\n    root: {root}\n    runtime:\n      type: native\n",
    )

    async def scenario():
        nodes = [node(tmp_path, name) for name in ("authority", "worker1", "worker2")]
        authority = nodes[0]
        try:
            await authority.initialize({"name": "lab"})
            for worker in nodes[1:]:
                await worker.join({"invite": authority.invitation({})})
            planned = await nodes[1].authority_call(
                "plan", {"workflow": workflow([n.node_id for n in nodes])}
            )
            assert not (var / "logs/runs.jsonl").exists()
            assert not authority.store.db.execute("SELECT * FROM operations").fetchall()
            result = await nodes[2].authority_call(
                "apply", planned | {"confirm": planned["digest"]}
            )
            final = await completed(authority, result["operation_id"])
            assert final["state"] == "succeeded", final
            assert len(final["results"]) == 6
            records = [
                json.loads(line)
                for line in (var / "logs/runs.jsonl").read_text().splitlines()
            ]
            assert len(records) == 9  # check, deploy, health check on each node
            assert {r["operation_id"] for r in records} == {result["operation_id"]}
            assert all(r["parent_run_id"] and r["run_id"] for r in records)
            assert all("arguments" not in r and "output" not in r for r in records)
            repeated = await nodes[2].authority_call(
                "apply", planned | {"confirm": planned["digest"]}
            )
            assert repeated == final
            assert len((var / "logs/runs.jsonl").read_text().splitlines()) == 9
            assert authority.store.get("lease") is None
        finally:
            await asyncio.gather(*(n.close() for n in nodes))

    asyncio.run(scenario())


def test_lost_response_is_unknown_and_reconcile_never_reexecutes(tmp_path, monkeypatch):
    root = tmp_path / "program"
    write_native_command(root, "sample")
    configure_environment(
        monkeypatch,
        tmp_path,
        f"programs:\n  sample:\n    root: {root}\n    runtime:\n      type: native\n",
    )

    async def scenario():
        authority, worker = node(tmp_path, "authority"), node(tmp_path, "worker")
        try:
            await authority.initialize({"name": "lab"})
            await worker.join({"invite": authority.invitation({})})
            planned = await authority.workflows.plan(workflow([worker.node_id]))
            actual = authority.workflows.peer

            async def drop_reply(node_id, method, params):
                result = await actual(node_id, method, params)
                if method == "execute":
                    raise IPCError("unavailable")
                return result

            monkeypatch.setattr(authority.workflows, "peer", drop_reply)
            operation = authority.workflows.apply(
                planned | {"confirm": planned["digest"]}
            )
            final = await completed(authority, operation["operation_id"])
            assert final["state"] == "unknown"
            assert len(final["results"]) == 1  # dependent mutation never dispatched
            second = json.loads(json.dumps(planned))
            second["plan"]["operation_id"] = str(uuid4())
            second["digest"] = digest(second["plan"])
            with pytest.raises(IPCError, match="operation_in_progress"):
                authority.workflows.apply(second | {"confirm": second["digest"]})
            reconciled = await authority.workflows.reconcile(operation["operation_id"])
            assert reconciled["state"] == "cancelled"
            assert reconciled["results"][0]["state"] == "succeeded"
            assert authority.store.get("lease") is None
            assert (
                len(worker.store.db.execute("SELECT * FROM executions").fetchall()) == 1
            )
        finally:
            await asyncio.gather(authority.close(), worker.close())

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "patch",
    [
        {"id": ""},
        {"id": "check:verify"},
        {"extra": True},
        {"mutates": "yes"},
        {"needs": "first"},
        {"arguments": [1]},
        {"arguments": ["\0"]},
        {"targets": {}},
        {"targets": {"nodes": "all"}},
        {"targets": {"nodes": []}},
        {"max_parallel": 0},
        {"timeout_seconds": True},
        {"rollout": {"other": 1}},
        {"verify": {"other": "health"}},
        {"needs": ["missing"]},
    ],
)
def test_invalid_steps_fail_before_planning(patch):
    from atlas.cluster.workflow import steps_from

    node_id = str(uuid4())
    members = [{"node_id": node_id, "state": "active", "labels": {}}]
    document = workflow([node_id])
    document["steps"] = [document["steps"][0] | patch]
    with pytest.raises(IPCError):
        steps_from(document, members)


def test_workflow_groups_and_dag_validation():
    from atlas.cluster.workflow import steps_from

    node_id = str(uuid4())
    members = [{"node_id": node_id, "state": "active", "labels": {"group": "app"}}]
    document = workflow([node_id])
    document["steps"][0]["targets"] = {"group": "app"}
    assert steps_from(document, members)[0]["targets"] == [node_id]
    for invalid in (
        {},
        document | {"steps": []},
        document | {"steps": ["bad"]},
        document | {"steps": [document["steps"][0]] * 2},
    ):
        with pytest.raises(IPCError):
            steps_from(invalid, members)


def test_plan_freshness_confirmation_and_grants(tmp_path, monkeypatch):
    import time
    from copy import deepcopy

    root = tmp_path / "program"
    write_native_command(root, "sample")
    configure_environment(
        monkeypatch,
        tmp_path,
        f"programs:\n  sample:\n    root: {root}\n    runtime:\n      type: native\n",
    )

    async def scenario():
        authority = node(tmp_path, "authority")
        try:
            await authority.initialize({"name": "lab"})
            engine = authority.workflows
            planned = await engine.plan(workflow([authority.node_id]))
            with pytest.raises(IPCError, match="unknown_operation"):
                engine.operation(str(uuid4()))
            with pytest.raises(IPCError, match="invalid_digest"):
                engine.apply(planned | {"confirm": "wrong"})
            expired = deepcopy(planned["plan"])
            expired["expires"] = time.time() - 1
            with pytest.raises(IPCError, match="stale_plan"):
                engine.apply(
                    {
                        "plan": expired,
                        "digest": digest(expired),
                        "confirm": digest(expired),
                    }
                )
            wrong_permission = deepcopy(planned["plan"])
            wrong_permission["required_permission"] = "read"
            with pytest.raises(IPCError, match="invalid_plan"):
                engine.apply(
                    {
                        "plan": wrong_permission,
                        "digest": digest(wrong_permission),
                        "confirm": digest(wrong_permission),
                    }
                )
            invalid = deepcopy(planned["plan"])
            invalid["steps"][0]["targets"] *= 2
            with pytest.raises(IPCError, match="invalid_plan"):
                engine.apply(
                    {
                        "plan": invalid,
                        "digest": digest(invalid),
                        "confirm": digest(invalid),
                    }
                )
            with pytest.raises(IPCError, match="stale_fence"):
                engine.grant({})
            result = engine.apply(planned | {"confirm": planned["digest"]})
            params = {
                "operation_id": result["operation_id"],
                "epoch": result["epoch"],
                "digest": result["digest"],
                "step_id": "check",
                "node_id": authority.node_id,
            }
            # Apply acknowledgement does not authorize an unscheduled step.
            with pytest.raises(IPCError, match="stale_fence"):
                engine.grant(params)
            await completed(authority, result["operation_id"])
            assert (
                await authority.local(
                    "operation.inspect", {"operation_id": result["operation_id"]}, None
                )
            )["state"] == "succeeded"
            assert (
                await authority.local(
                    "operation.reconcile",
                    {"operation_id": result["operation_id"]},
                    None,
                )
            )["state"] == "succeeded"
            conflict = deepcopy(planned["plan"])
            conflict["name"] = "other"
            with pytest.raises(IPCError, match="operation_conflict"):
                engine.apply(
                    {
                        "plan": conflict,
                        "digest": digest(conflict),
                        "confirm": digest(conflict),
                    }
                )
            with authority.store.db:
                authority.store.set("lease", params | {"expires": time.time() + 300})
                authority.store.db.execute("UPDATE operations SET state='running'")
            with pytest.raises(IPCError, match="step_not_authorized"):
                engine.grant(params)
            with pytest.raises(IPCError, match="forbidden"):
                await engine.request(
                    "lease.validate",
                    params | {"node_id": "different"},
                    authority.node_id,
                )
            with pytest.raises(IPCError, match="forbidden"):
                await engine.request("execute", params, "different")
        finally:
            await authority.close()

    asyncio.run(scenario())


def test_parallel_duplicate_worker_requests_launch_once(tmp_path, monkeypatch):
    root = tmp_path / "program"
    marker = tmp_path / "launches"
    write_native_command(
        root, "sample", f"#!/bin/sh\nprintf x >> '{marker}'\nsleep 0.1\n"
    )
    configure_environment(
        monkeypatch,
        tmp_path,
        f"programs:\n  sample:\n    root: {root}\n    runtime:\n      type: native\n",
    )

    async def scenario():
        authority, worker = node(tmp_path, "authority"), node(tmp_path, "worker")
        try:
            await authority.initialize({"name": "lab"})
            await worker.join({"invite": authority.invitation({})})
            doc = workflow([worker.node_id])
            doc["steps"] = doc["steps"][:1]
            planned = await authority.workflows.plan(doc)
            peer = authority.workflows.peer

            async def duplicate(node_id, method, params):
                if method != "execute":
                    return await peer(node_id, method, params)
                results = await asyncio.gather(
                    worker.workflows.execution(params),
                    worker.workflows.execution(params),
                )
                done = next(value for value in results if value["state"] == "succeeded")
                assert await worker.workflows.execution(params) == done
                return done

            monkeypatch.setattr(authority.workflows, "peer", duplicate)
            result = authority.workflows.apply(planned | {"confirm": planned["digest"]})
            assert (await completed(authority, result["operation_id"]))[
                "state"
            ] == "succeeded"
            assert marker.read_text() == "x"
            with pytest.raises(IPCError, match="unknown_method"):
                await worker.authority_call("not-a-method", {})
            with pytest.raises(IPCError, match="unknown_method"):
                await worker.workflows.request("plan", {}, authority.node_id)
        finally:
            await asyncio.gather(authority.close(), worker.close())

    asyncio.run(scenario())


def test_source_change_failure_and_expired_lease_stop_dependent_work(
    tmp_path, monkeypatch
):
    from unittest.mock import AsyncMock

    root = tmp_path / "program"
    command = write_native_command(root, "sample")
    configure_environment(
        monkeypatch,
        tmp_path,
        f"programs:\n  sample:\n    root: {root}\n    runtime:\n      type: native\n",
    )

    async def scenario():
        authority = node(tmp_path, "authority")
        try:
            await authority.initialize({"name": "lab"})
            engine = authority.workflows
            planned = await engine.plan(workflow([authority.node_id]))
            command.write_text("#!/bin/sh\nexit 1\n")
            result = engine.apply(planned | {"confirm": planned["digest"]})
            assert (await completed(authority, result["operation_id"]))[
                "state"
            ] == "unknown"
            # No worker journal proves completion after a pre-launch source mismatch.
            assert (await engine.reconcile(result["operation_id"]))[
                "state"
            ] == "unknown"
            original_peer = engine.peer
            monkeypatch.setattr(
                engine, "peer", AsyncMock(side_effect=IPCError("unavailable"))
            )
            assert (await engine.reconcile(result["operation_id"]))[
                "state"
            ] == "unknown"
            monkeypatch.setattr(engine, "peer", original_peer)
            with authority.store.db:
                authority.store.set("lease", None)
            planned = await engine.plan(workflow([authority.node_id]))
            result = engine.apply(planned | {"confirm": planned["digest"]})
            final = await completed(authority, result["operation_id"])
            assert final["state"] == "failed"
            assert final["results"][0]["exit_code"] == 1
            assert len(final["results"]) == 1
            planned = await engine.plan(workflow([authority.node_id]))
            result = engine.apply(planned | {"confirm": planned["digest"]})
            with authority.store.db:
                lease = authority.store.get("lease")
                authority.store.set("lease", lease | {"expires": 0})
            assert (await completed(authority, result["operation_id"]))[
                "state"
            ] == "unknown"
        finally:
            await authority.close()

    asyncio.run(scenario())


def test_health_source_change_blocks_queued_targets_and_keeps_unknown(
    tmp_path, monkeypatch
):
    root = tmp_path / "program"
    health = write_native_command(root, "health")
    write_native_command(
        root, "sample", f"#!/bin/sh\nprintf '#!/bin/sh\\nexit 1\\n' > '{health}'\n"
    )
    configure_environment(
        monkeypatch,
        tmp_path,
        f"programs:\n  sample:\n    root: {root}\n    runtime:\n      type: native\n",
    )

    async def scenario():
        authority, worker = node(tmp_path, "authority"), node(tmp_path, "worker")
        try:
            await authority.initialize({"name": "lab"})
            await worker.join({"invite": authority.invitation({})})
            doc = workflow(
                [authority.node_id, worker.node_id], verify={"command": "health"}
            )
            # Preflight must not edit the health entry point.
            doc["steps"][0]["command"] = "health"
            planned = await authority.workflows.plan(doc)
            result = authority.workflows.apply(planned | {"confirm": planned["digest"]})
            final = await completed(authority, result["operation_id"])
            assert final["state"] == "unknown"
            assert len(final["results"]) == 3
            assert sum(row["state"] == "succeeded" for row in final["results"]) == 2
            assert (await authority.workflows.reconcile(result["operation_id"]))[
                "state"
            ] == "unknown"
        finally:
            await asyncio.gather(authority.close(), worker.close())

    asyncio.run(scenario())


def test_worker_shutdown_cancels_executor_and_releases_no_unknown_lease(
    tmp_path, monkeypatch
):
    root = tmp_path / "program"
    started = tmp_path / "started"
    write_native_command(root, "sample", f"#!/bin/sh\ntouch '{started}'\nsleep 30\n")
    configure_environment(
        monkeypatch,
        tmp_path,
        f"programs:\n  sample:\n    root: {root}\n    runtime:\n      type: native\n",
    )

    async def scenario():
        authority = node(tmp_path, "authority")
        await authority.initialize({"name": "lab"})
        planned = await authority.workflows.plan(workflow([authority.node_id]))
        result = authority.workflows.apply(planned | {"confirm": planned["digest"]})
        async with asyncio.timeout(5):
            while not started.exists():
                await asyncio.sleep(0.01)
        directory = authority.directory
        await authority.close()
        restored = node(tmp_path, "other")
        try:
            from atlas.cluster.store import Store

            store = Store(directory)
            try:
                assert store.get("lease")["operation_id"] == result["operation_id"]
                assert (
                    store.db.execute("SELECT state FROM executions").fetchone()[0]
                    == "unknown"
                )
            finally:
                store.close()
        finally:
            await restored.close()

    asyncio.run(scenario())


def test_executor_without_completion_record_is_unknown_and_hung_shutdown_is_killed(
    tmp_path, monkeypatch
):
    from unittest.mock import AsyncMock, Mock

    configure_environment(monkeypatch, tmp_path, "programs: {}\n")

    async def scenario():
        local = node(tmp_path, "local")
        try:
            # A launcher failing before the Executor writes a record proves no outcome.
            result = await local.workflows.run_command(
                "missing", [], 1, str(uuid4()), "step"
            )
            assert result["state"] == "unknown"
            process = Mock(returncode=None)
            entered = asyncio.Event()

            async def wait():
                entered.set()
                await asyncio.Event().wait()

            process.wait = AsyncMock(side_effect=wait)
            monkeypatch.setattr(
                asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
            )
            original_wait_for = asyncio.wait_for

            async def expire(awaitable, timeout):
                awaitable.close()
                process.wait = AsyncMock(return_value=-9)
                raise TimeoutError

            monkeypatch.setattr(asyncio, "wait_for", expire)
            task = asyncio.create_task(
                local.workflows.run_command("sample", [], 1, str(uuid4()), "step")
            )
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            process.terminate.assert_called_once()
            process.kill.assert_called_once()
            process.wait.assert_awaited_once()
            monkeypatch.setattr(asyncio, "wait_for", original_wait_for)
        finally:
            await local.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("late_state", ["succeeded", "unknown"])
def test_overlapping_reconciliations_cannot_release_a_new_operation(
    tmp_path, monkeypatch, late_state
):
    root = tmp_path / "program"
    write_native_command(root, "sample")
    configure_environment(
        monkeypatch,
        tmp_path,
        f"programs:\n  sample:\n    root: {root}\n    runtime:\n      type: native\n",
    )

    async def scenario():
        authority = node(tmp_path, "authority")
        try:
            await authority.initialize({"name": "lab"})
            engine, store = authority.workflows, authority.store
            planned = await engine.plan(workflow([authority.node_id]))
            old = str(uuid4())
            with store.db:
                store.db.execute(
                    "INSERT INTO operations VALUES (?,?,?,'unknown',1)",
                    (old, "old", "{}"),
                )
                store.db.execute(
                    "INSERT INTO results VALUES (?,?,?,'unknown',NULL,NULL)",
                    (old, "step", authority.node_id),
                )
                store.set("epoch", 1)
                store.set("lease", {"operation_id": old, "epoch": 1, "expires": 0})
            both_started = asyncio.Event()
            release = [asyncio.Event(), asyncio.Event()]
            calls = 0

            async def observe(*args):
                nonlocal calls
                index = calls
                calls += 1
                if calls == 2:
                    both_started.set()
                await release[index].wait()
                return {
                    "state": "succeeded" if index == 0 else late_state,
                    "exit_code": 0,
                    "run_id": "run",
                }

            async def hold_new_operation(*args):
                await asyncio.Event().wait()

            monkeypatch.setattr(engine, "peer", observe)
            monkeypatch.setattr(engine, "coordinate", hold_new_operation)
            first = asyncio.create_task(engine.reconcile(old))
            second = asyncio.create_task(engine.reconcile(old))
            await both_started.wait()
            release[0].set()
            assert (await first)["state"] == "cancelled"
            newer = engine.apply(planned | {"confirm": planned["digest"]})
            reservation = store.get("lease")
            assert reservation["operation_id"] == newer["operation_id"]
            assert reservation["epoch"] == 2
            release[1].set()
            assert (await second)["state"] == "cancelled"
            assert store.get("lease") == reservation
        finally:
            await authority.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "reservation",
    [None, {"operation_id": "other", "epoch": 1}, {"operation_id": "same", "epoch": 2}],
)
def test_reconcile_only_releases_its_own_fencing_epoch(tmp_path, reservation):
    async def scenario():
        local = node(tmp_path, "local")
        try:
            operation_id = str(uuid4())
            lease = reservation
            if lease is not None and lease["operation_id"] == "same":
                lease = lease | {"operation_id": operation_id}
            with local.store.db:
                local.store.db.execute(
                    "INSERT INTO operations VALUES (?,?,?,'unknown',1)",
                    (operation_id, "digest", "{}"),
                )
                local.store.db.execute(
                    "INSERT INTO results VALUES (?,?,?,'succeeded',0,'run')",
                    (operation_id, "step", local.node_id),
                )
                local.store.set("lease", lease)
            assert (await local.workflows.reconcile(operation_id))[
                "state"
            ] == "cancelled"
            assert local.store.get("lease") == lease
        finally:
            await local.close()

    asyncio.run(scenario())
