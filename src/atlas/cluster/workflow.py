"""Validated plans and durable, authority-fenced command execution."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import time
from uuid import NAMESPACE_URL, uuid4, uuid5

from atlas.catalog import resolve_command
from atlas.config import load_config
from atlas.paths import get_paths
from atlas_core.ipc import IPCError

from . import network
from .node import identifier, text

TERMINAL = {"succeeded", "failed", "timed_out", "cancelled"}


def digest(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def positive(value, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise IPCError("invalid_limit")
    return value


def command_identity(name: str) -> dict:
    paths = get_paths()
    command = resolve_command(load_config(paths.config_file), name)
    return {
        "name": name,
        "program": command.program.name,
        "root": str(command.program.root),
        "path": str(command.path),
        "type": command.type,
        "sha256": hashlib.sha256(command.path.read_bytes()).hexdigest(),
        "registration_sha256": hashlib.sha256(
            paths.config_file.read_bytes()
        ).hexdigest(),
    }


def steps_from(workflow: dict, members: list[dict]) -> list[dict]:
    if workflow.get("schema") != "atlas.workflow/v1" or workflow.keys() - {
        "schema",
        "name",
        "steps",
    }:
        raise IPCError("invalid_workflow")
    text(workflow.get("name"))
    raw = workflow.get("steps")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 100:
        raise IPCError("invalid_workflow")
    steps = {}
    for value in raw:
        if not isinstance(value, dict) or value.keys() - {
            "id",
            "needs",
            "targets",
            "command",
            "arguments",
            "mutates",
            "max_parallel",
            "rollout",
            "verify",
            "timeout_seconds",
        }:
            raise IPCError("invalid_step")
        step_id = text(value.get("id"))
        if (
            not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}", step_id)
            or step_id in steps
            or type(value.get("mutates")) is not bool
        ):
            raise IPCError("invalid_step")
        needs, args = value.get("needs", []), value.get("arguments", [])
        if (
            not isinstance(needs, list)
            or not isinstance(args, list)
            or any(not isinstance(v, str) or "\0" in v for v in args)
        ):
            raise IPCError("invalid_step")
        needs = [text(v) for v in needs]
        selector = value.get("targets")
        if (
            not isinstance(selector, dict)
            or len(selector) != 1
            or not selector.keys() <= {"group", "nodes"}
        ):
            raise IPCError("invalid_targets")
        if "group" in selector:
            group = text(selector["group"])
            targets = [
                m["node_id"]
                for m in members
                if m["state"] == "active" and m["labels"].get("group") == group
            ]
        else:
            if not isinstance(selector["nodes"], list):
                raise IPCError("invalid_targets")
            targets = list(dict.fromkeys(identifier(v) for v in selector["nodes"]))
        available = {m["node_id"] for m in members if m["state"] == "active"}
        if not targets or not set(targets) <= available:
            raise IPCError("invalid_targets")
        parallel = positive(value.get("max_parallel", 1), 32)
        rollout = value.get("rollout")
        if rollout is not None:
            if not isinstance(rollout, dict) or rollout.keys() - {
                "max_parallel",
                "max_unavailable",
            }:
                raise IPCError("invalid_rollout")
            parallel = min(
                positive(rollout.get("max_parallel", 1), 32),
                positive(rollout.get("max_unavailable", 1), 32),
            )
        verify = value.get("verify")
        if verify is not None:
            if not isinstance(verify, dict) or verify.keys() - {
                "command",
                "timeout_seconds",
            }:
                raise IPCError("invalid_verification")
            verify = {
                "command": text(verify.get("command")),
                "timeout_seconds": positive(verify.get("timeout_seconds", 30), 120),
            }
        steps[step_id] = {
            "id": step_id,
            "needs": needs,
            "targets": sorted(targets),
            "command": text(value.get("command")),
            "arguments": args,
            "mutates": value["mutates"],
            "max_parallel": parallel,
            "timeout_seconds": positive(value.get("timeout_seconds", 120), 120),
            "verify": verify,
        }
    ordered = []
    while steps:
        ready = [
            step
            for step in steps.values()
            if set(step["needs"]) <= {item["id"] for item in ordered}
        ]
        if not ready:
            raise IPCError("invalid_dependencies")
        for step in ready:
            ordered.append(step)
            del steps[step["id"]]
    return ordered


class WorkflowEngine:
    def __init__(self, node) -> None:
        self.node = node
        self.store = node.store
        self.tasks: set[asyncio.Task] = set()
        self.store.db.execute("""CREATE TABLE IF NOT EXISTS results (
            operation_id TEXT NOT NULL, step_id TEXT NOT NULL, node_id TEXT NOT NULL,
            state TEXT NOT NULL, exit_code INTEGER, run_id TEXT,
            PRIMARY KEY(operation_id,step_id,node_id))""")
        with self.store.db:
            self.store.db.execute(
                "UPDATE results SET state='unknown' WHERE state IN ('prepared','running')"
            )

    async def close(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def peer(self, node_id: str, method: str, params: dict) -> dict:
        if node_id == self.node.node_id:
            return await self.node.remote(
                method, params, (node_id, self.store.get("certificate"))
            )
        member = self.node.authorize(node_id, "execute")
        return await network.call(
            member["endpoint"], node_id, self.node.tls(), method, params, timeout=260
        )

    async def plan(self, workflow: dict) -> dict:
        steps = steps_from(workflow, self.store.members())
        for step in steps:
            step["sources"] = {}
            for node_id in step["targets"]:
                step["sources"][node_id] = await self.peer(
                    node_id, "command.inspect", {"command": step["command"]}
                )
                if step["verify"]:
                    step.setdefault("verification_sources", {})[
                        node_id
                    ] = await self.peer(
                        node_id,
                        "command.inspect",
                        {"command": step["verify"]["command"]},
                    )
        plan = {
            "schema": "atlas.cluster-plan/v1",
            "cluster_id": self.store.get("cluster_id"),
            "operation_id": str(uuid4()),
            "name": workflow["name"],
            "expires": time.time() + 600,
            "revision": self.store.get("revision"),
            "required_permission": "execute",
            "steps": steps,
        }
        return {"plan": plan, "digest": digest(plan)}

    def operation(self, operation_id: str) -> dict:
        row = self.store.db.execute(
            "SELECT operation_id,digest,state,epoch FROM operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if row is None:
            raise IPCError("unknown_operation")
        return dict(row) | {
            "results": [
                dict(row)
                for row in self.store.db.execute(
                    "SELECT * FROM results WHERE operation_id=? ORDER BY step_id,node_id",
                    (operation_id,),
                )
            ]
        }

    def apply(self, params: dict) -> dict:
        plan, supplied = params.get("plan"), params.get("digest")
        if (
            not isinstance(plan, dict)
            or supplied != digest(plan)
            or params.get("confirm") != supplied
        ):
            raise IPCError("invalid_digest")
        if (
            plan.get("schema") != "atlas.cluster-plan/v1"
            or plan.get("cluster_id") != self.store.get("cluster_id")
            or plan.get("revision") != self.store.get("revision")
            or plan.get("expires", 0) <= time.time()
        ):
            raise IPCError("stale_plan")
        operation_id = identifier(plan.get("operation_id"))
        if (
            operation_id != plan["operation_id"]
            or plan.get("required_permission") != "execute"
        ):
            raise IPCError("invalid_plan")
        # Plans come back through an untrusted file. Revalidate every executable field.
        normalized = steps_from(
            {
                "schema": "atlas.workflow/v1",
                "name": plan.get("name"),
                "steps": [
                    {
                        key: value
                        for key, value in step.items()
                        if key not in {"sources", "verification_sources"}
                    }
                    | {"targets": {"nodes": step["targets"]}}
                    for step in plan["steps"]
                ],
            },
            self.store.members(),
        )
        for original, checked in zip(plan["steps"], normalized, strict=True):
            if {
                k: v
                for k, v in original.items()
                if k not in {"sources", "verification_sources"}
            } != checked:
                raise IPCError("invalid_plan")
        with self.store.db:
            existing = self.store.db.execute(
                "SELECT digest FROM operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if existing:
                if existing[0] != supplied:
                    raise IPCError("operation_conflict")
                return self.operation(operation_id)
            if self.store.get("lease") is not None:
                raise IPCError("operation_in_progress")
            epoch = self.store.get("epoch") + 1
            self.store.set("epoch", epoch)
            self.store.set(
                "lease",
                {
                    "operation_id": operation_id,
                    "epoch": epoch,
                    "expires": time.time() + 300,
                },
            )
            self.store.db.execute(
                "INSERT INTO operations VALUES (?,?,?,?,?)",
                (operation_id, supplied, json.dumps(plan), "prepared", epoch),
            )
            self.store.event("operation_prepared", operation_id)
        task = asyncio.create_task(
            self.coordinate(plan, supplied, epoch),
            name=f"cluster-operation-{operation_id}",
        )
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return self.operation(operation_id)

    def grant(self, params: dict) -> dict:
        lease = self.store.get("lease")
        if (
            lease is None
            or lease["operation_id"] != params.get("operation_id")
            or lease["epoch"] != params.get("epoch")
            or lease["expires"] <= time.time()
        ):
            raise IPCError("stale_fence")
        row = self.store.db.execute(
            "SELECT plan,digest,state FROM operations WHERE operation_id=?",
            (lease["operation_id"],),
        ).fetchone()
        if row["state"] != "running" or row["digest"] != params.get("digest"):
            raise IPCError("stale_fence")
        result = self.store.db.execute(
            "SELECT state FROM results WHERE operation_id=? AND step_id=? AND node_id=?",
            (lease["operation_id"], params.get("step_id"), params.get("node_id")),
        ).fetchone()
        if result is None or result[0] != "running":
            raise IPCError("step_not_authorized")
        return {"plan": json.loads(row["plan"]), "epoch": lease["epoch"]}

    async def coordinate(self, plan: dict, plan_digest: str, epoch: int) -> None:
        operation_id = plan["operation_id"]
        outcome = "unknown"
        try:
            with self.store.db:
                self.store.db.execute(
                    "UPDATE operations SET state='running' WHERE operation_id=?",
                    (operation_id,),
                )
            for step in plan["steps"]:
                failed = await self.dispatch_step(plan, step, plan_digest, epoch)
                if failed.is_set():
                    states = {
                        r[0]
                        for r in self.store.db.execute(
                            "SELECT state FROM results WHERE operation_id=?",
                            (operation_id,),
                        )
                    }
                    outcome = "unknown" if "unknown" in states else "failed"
                    break
            else:
                outcome = "succeeded"
        except Exception:
            outcome = "unknown"
        finally:
            with self.store.db:
                self.store.db.execute(
                    "UPDATE operations SET state=? WHERE operation_id=?",
                    (outcome, operation_id),
                )
                self.store.event(f"operation_{outcome}", operation_id)
                if outcome != "unknown":
                    self.store.set("lease", None)

    async def dispatch_step(
        self, plan: dict, step: dict, plan_digest: str, epoch: int
    ) -> asyncio.Event:
        operation_id = plan["operation_id"]
        semaphore = asyncio.Semaphore(step["max_parallel"])
        failed = asyncio.Event()

        async def dispatch(node_id: str) -> None:
            async with semaphore:
                if failed.is_set():
                    return
                self.node.authorize(node_id, "execute")
                lease = self.store.get("lease")
                if lease["expires"] <= time.time():
                    raise IPCError("stale_fence")
                with self.store.db:
                    self.store.set(
                        "lease",
                        {
                            "operation_id": operation_id,
                            "epoch": epoch,
                            "expires": time.time() + 300,
                        },
                    )
                    self.store.db.execute(
                        "INSERT INTO results VALUES (?,?,?,?,NULL,NULL)",
                        (operation_id, step["id"], node_id, "running"),
                    )
                result = {"state": "unknown", "exit_code": None, "run_id": None}
                try:
                    result = await self.peer(
                        node_id,
                        "execute",
                        {
                            "operation_id": operation_id,
                            "digest": plan_digest,
                            "epoch": epoch,
                            "step_id": step["id"],
                            "node_id": node_id,
                        },
                    )
                except IPCError:
                    pass  # A disconnected worker may already have performed side effects.
                finally:
                    with self.store.db:
                        self.store.db.execute(
                            "UPDATE results SET state=?,exit_code=?,run_id=? WHERE operation_id=? AND step_id=? AND node_id=?",
                            (
                                result["state"],
                                result["exit_code"],
                                result["run_id"],
                                operation_id,
                                step["id"],
                                node_id,
                            ),
                        )
                if result["state"] != "succeeded":
                    failed.set()

        async with asyncio.TaskGroup() as group:
            for node_id in step["targets"]:
                group.create_task(dispatch(node_id))
        return failed

    async def execution(self, params: dict) -> dict:
        operation_id, step_id = (
            identifier(params.get("operation_id")),
            text(params.get("step_id")),
        )
        key = (operation_id, step_id, self.node.node_id)
        row = self.store.db.execute(
            "SELECT state,exit_code,run_id FROM executions WHERE operation_id=? AND step_id=? AND node_id=?",
            key,
        ).fetchone()
        if row:
            return dict(row)
        granted = await self.node.authority_call(
            "lease.validate", params | {"node_id": self.node.node_id}
        )
        step = next(item for item in granted["plan"]["steps"] if item["id"] == step_id)
        if command_identity(step["command"]) != step["sources"][self.node.node_id]:
            raise IPCError("source_changed")
        with self.store.db:
            if not self.store.db.execute(
                "INSERT OR IGNORE INTO executions VALUES (?,?,?,'running',NULL,NULL)",
                key,
            ).rowcount:
                return dict(
                    self.store.db.execute(
                        "SELECT state,exit_code,run_id FROM executions WHERE operation_id=? AND step_id=? AND node_id=?",
                        key,
                    ).fetchone()
                )
        result = {"state": "unknown", "exit_code": None, "run_id": None}
        try:
            result = await self.run_command(
                step["command"],
                step["arguments"],
                step["timeout_seconds"],
                operation_id,
                step_id,
            )
            if result["state"] == "succeeded" and step["verify"]:
                result = {
                    "state": "unknown",
                    "exit_code": None,
                    "run_id": result["run_id"],
                }
                await self.node.authority_call(
                    "lease.validate", params | {"node_id": self.node.node_id}
                )
                verify = step["verify"]
                if (
                    command_identity(verify["command"])
                    != step["verification_sources"][self.node.node_id]
                ):
                    raise IPCError("source_changed")
                result = await self.run_command(
                    verify["command"],
                    [],
                    verify["timeout_seconds"],
                    operation_id,
                    step_id + ":verify",
                )
            return result
        finally:
            with self.store.db:
                self.store.db.execute(
                    "UPDATE executions SET state=?,exit_code=?,run_id=? WHERE operation_id=? AND step_id=? AND node_id=?",
                    (result["state"], result["exit_code"], result["run_id"], *key),
                )

    async def run_command(
        self,
        command: str,
        args: list[str],
        timeout: int,
        operation_id: str,
        step_id: str,
    ) -> dict:
        parent_id = str(
            uuid5(NAMESPACE_URL, f"{operation_id}/{step_id}/{self.node.node_id}")
        )
        environment = {
            k: v
            for k, v in os.environ.items()
            if k
            not in {
                "ATLAS_RUN_ID",
                "ATLAS_RUN_TEMP_DIR",
                "ATLAS_CONTEXT_FILE",
                "ATLAS_OPERATION_ID",
            }
        }
        environment.update(ATLAS_RUN_ID=parent_id, ATLAS_OPERATION_ID=operation_id)
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "atlas.cli",
            "run",
            "--timeout",
            str(timeout),
            command,
            "--",
            *args,
            env=environment,
            start_new_session=True,
        )
        try:
            code = await process.wait()
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except TimeoutError:
                    process.kill()
                    await process.wait()
        run_id = None
        path = get_paths().run_log
        if path.exists():
            for line in path.read_text().splitlines():
                record = json.loads(line)
                if record["parent_run_id"] == parent_id:
                    run_id = record["run_id"]
        state = "succeeded" if code == 0 else "timed_out" if code == 124 else "failed"
        if run_id is None:
            state = "unknown"
        return {"state": state, "exit_code": code, "run_id": run_id}

    async def reconcile(self, operation_id: str) -> dict:
        operation = self.operation(operation_id)
        if operation["state"] != "unknown":
            return operation
        for result in operation["results"]:
            if result["state"] not in TERMINAL:
                try:
                    observed = await self.peer(
                        result["node_id"], "execution.inspect", result
                    )
                except IPCError:
                    continue
                if observed["state"] in TERMINAL:
                    with self.store.db:
                        current = self.operation(operation_id)
                        if current["state"] != "unknown":
                            return current
                        self.store.db.execute(
                            "UPDATE results SET state=?,exit_code=?,run_id=? WHERE operation_id=? AND step_id=? AND node_id=?",
                            (
                                observed["state"],
                                observed["exit_code"],
                                observed["run_id"],
                                operation_id,
                                result["step_id"],
                                result["node_id"],
                            ),
                        )
        with self.store.db:
            current = self.operation(operation_id)
            if current["state"] != "unknown":
                return current
            if current["results"] and all(
                r["state"] in TERMINAL for r in current["results"]
            ):
                # Reconciliation never launches omitted dependent steps. Its response
                # can arrive after another reconciliation released this reservation.
                self.store.db.execute(
                    "UPDATE operations SET state='cancelled' WHERE operation_id=?",
                    (operation_id,),
                )
                lease = self.store.get("lease")
                if (
                    lease is not None
                    and lease["operation_id"] == operation_id
                    and lease["epoch"] == current["epoch"]
                ):
                    self.store.set("lease", None)
                self.store.event("operation_reconciled", operation_id)
        return self.operation(operation_id)

    async def request(self, method: str, params: dict, caller: str) -> dict:
        if method == "command.inspect":
            return command_identity(text(params.get("command")))
        if method == "execute":
            if caller != self.store.get("authority_id"):
                raise IPCError("forbidden")
            return await self.execution(params)
        if method == "execution.inspect":
            row = self.store.db.execute(
                "SELECT state,exit_code,run_id FROM executions WHERE operation_id=? AND step_id=? AND node_id=?",
                (
                    identifier(params.get("operation_id")),
                    text(params.get("step_id")),
                    self.node.node_id,
                ),
            ).fetchone()
            return (
                dict(row)
                if row
                else {"state": "unknown", "exit_code": None, "run_id": None}
            )
        if self.node.authority:
            if method == "plan":
                return await self.plan(params.get("workflow", {}))
            if method == "apply":
                return self.apply(params)
            if method == "lease.validate":
                if params.get("node_id") != caller:
                    raise IPCError("forbidden")
                return self.grant(params)
            if method == "operation.inspect":
                return self.operation(identifier(params.get("operation_id")))
            if method == "operation.reconcile":
                return await self.reconcile(identifier(params.get("operation_id")))
        raise IPCError("unknown_method")
