from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from atlas_host_operations.configurators import FakeHostConfigurator
from atlas_host_operations.controller import build_host_plan
from atlas_host_operations.models import HostOperationPlan, RegistryResource
from atlas_host_operations.providers import FakeCloudProvider
from atlas_host_operations.readiness import FakeReadinessChecker
from atlas_host_operations.registry import HTTPResponse, InMemoryRegistryClient


@dataclass
class HostFixture:
    root: Path
    project: Path
    host_spec: Path
    registry_profile: Path
    provider_definition: Path
    provider_input: Path
    registry: InMemoryRegistryClient
    provider: FakeCloudProvider
    configurator: FakeHostConfigurator
    readiness: FakeReadinessChecker

    def plan(self) -> HostOperationPlan:
        return build_host_plan(
            self.host_spec,
            registry=self.registry,
            provider=self.provider,
            configurator=self.configurator,
            now=lambda: datetime.now(UTC),
            new_id=lambda: "fixture",
        )


def make_host_fixture(
    tmp_path: Path,
    *,
    provider_adapter: str = "fake-cloud",
    configuration_adapter: str = "fake-configurator",
) -> HostFixture:
    project = tmp_path / "provisioning"
    inventory = project / "inventories" / "site01" / "hosts.yml"
    inventory.parent.mkdir(parents=True)
    inventory.write_text("all:\n  hosts:\n    web01: {}\n", encoding="utf-8")
    playbooks = project / "playbooks"
    playbooks.mkdir()
    for name in ("bootstrap", "site"):
        (playbooks / f"{name}.yml").write_text(
            "---\n- hosts: all\n  tasks: []\n",
            encoding="utf-8",
        )
    (project / "ansible.cfg").write_text(
        "[defaults]\ninventory=inventories/site01/hosts.yml\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(
        ["git", "-C", str(project), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(project), "config", "user.name", "Test"],
        check=True,
    )
    subprocess.run(["git", "-C", str(project), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(project), "commit", "-qm", "fixture"],
        check=True,
    )
    registry_profile = tmp_path / "registry.yml"
    registry_profile.write_text(
        "schema: atlas.registry-profile/v1\n"
        "base_url: http://localhost:8787\n"
        "access:\n"
        "  development_identity: test\n",
        encoding="utf-8",
    )
    provider_definition = tmp_path / "provider.yml"
    provider_definition.write_text("provider: fake\n", encoding="utf-8")
    provider_input = tmp_path / "input.yml"
    provider_input.write_text("name: web01\n", encoding="utf-8")
    host_spec = tmp_path / "host.yml"
    host_spec.write_text(
        yaml.safe_dump(
            {
                "schema": "atlas.host-spec/v1",
                "kind": "HostCreate",
                "resource": {
                    "id": "host-web01",
                    "name": "web01",
                    "site": "site01",
                    "zone": "dmz",
                },
                "registry": {"profile": registry_profile.name},
                "provider": {
                    "adapter": provider_adapter,
                    "definition": provider_definition.name,
                    "input": provider_input.name,
                },
                "configuration": {
                    "adapter": configuration_adapter,
                    "project_root": str(project),
                    "target": "web01",
                    "bootstrap_playbook": "bootstrap",
                    "converge_playbook": "site",
                },
                "readiness": {
                    "address": "192.0.2.10",
                    "ssh_port": 22,
                    "ssh_user": "ops",
                    "require_cloud_init": True,
                    "require_guest_agent": True,
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    registry = InMemoryRegistryClient(
        [
            RegistryResource(
                id="resource-1",
                key="host-web01",
                kind="compute",
                name="web01",
                lifecycleState="absent",
                revision=1,
            )
        ]
    )
    return HostFixture(
        root=tmp_path,
        project=project,
        host_spec=host_spec,
        registry_profile=registry_profile,
        provider_definition=provider_definition,
        provider_input=provider_input,
        registry=registry,
        provider=FakeCloudProvider(name=provider_adapter),
        configurator=FakeHostConfigurator(name=configuration_adapter),
        readiness=FakeReadinessChecker(),
    )


@dataclass
class ScriptedTransport:
    responses: list[HTTPResponse | BaseException]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: int,
    ) -> HTTPResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": None if body is None else json.loads(body),
                "timeout_seconds": timeout_seconds,
            }
        )
        if not self.responses:
            raise AssertionError(f"no scripted response for {method} {url}")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def response(status: int, body: object = None) -> HTTPResponse:
    if body is None:
        return HTTPResponse(status, b"")
    return HTTPResponse(status, json.dumps(body).encode())


def resource_payload(
    *,
    lifecycle: str = "absent",
    revision: int = 1,
    binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "resource": {
            "id": "resource-1",
            "key": "host-web01",
            "kind": "compute",
            "name": "web01",
            "lifecycleState": lifecycle,
            "revision": revision,
        },
        "binding": binding,
    }


def operation_payload(
    plan: HostOperationPlan,
    *,
    operation_id: str = "op-1",
    status: str = "planned",
    revision: int = 1,
    step_status: str = "planned",
) -> dict[str, Any]:
    return {
        "operation": {
            "id": operation_id,
            "status": status,
            "revision": revision,
            "plan": {
                "kind": "host_create",
                "intent": {
                    "idempotencyKey": plan.metadata.idempotency_key,
                    "hostPlan": plan.as_artifact(),
                },
            },
        },
        "resources": [
            {
                "resourceKey": plan.resource.id,
                "sourceState": "absent",
                "targetState": "ready",
                "resourceRevision": 1,
                "lifecyclePath": [
                    "absent",
                    "allocated",
                    "bootstrapped",
                    "configured",
                    "ready",
                ],
            }
        ],
        "steps": [
            {
                "id": f"step-{position}",
                "name": phase.value,
                "status": step_status,
                "evidence": {},
                "revision": 1,
            }
            for position, phase in enumerate(plan.phases)
        ],
    }
