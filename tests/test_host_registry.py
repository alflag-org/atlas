from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from atlas_host_operations.controller import _evidence
from atlas_host_operations.errors import (
    InputError,
    RegistryAuthenticationError,
    RegistryConflictError,
    RegistryError,
    RegistryUnavailableError,
)
from atlas_host_operations.lifecycle import ProvisioningPhase, StepStatus
from atlas_host_operations.models import (
    ProviderEvidence,
    RegistryAuthority,
    RegistryResource,
)
from atlas_host_operations.registry import (
    HTTPRegistryClient,
    InMemoryRegistryClient,
    RegistryAccess,
    RegistryProfile,
    UrllibTransport,
    load_registry_client,
    resolve_secret_reference,
)
from pydantic import ValidationError

from .test_host_operations_support import (
    ScriptedTransport,
    make_host_fixture,
    operation_payload,
    resource_payload,
    response,
)


def _client(transport: ScriptedTransport) -> HTTPRegistryClient:
    return HTTPRegistryClient(
        "https://registry.example.test",
        access_client_id="client-id",
        access_client_secret="client-secret",
        timeout_seconds=17,
        transport=transport,
    )


def test_registry_profiles_and_secret_references(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REGISTRY_CLIENT_ID", "client-id")
    secret = tmp_path / "secret"
    secret.write_text("client-secret\n", encoding="utf-8")
    profile = tmp_path / "registry.yml"
    profile.write_text(
        "schema: atlas.registry-profile/v1\n"
        "base_url: https://registry.example.test/\n"
        "timeout_seconds: 20\n"
        "access:\n"
        "  client_id_ref: env:REGISTRY_CLIENT_ID\n"
        f"  client_secret_ref: file:{secret}\n",
        encoding="utf-8",
    )
    transport = ScriptedTransport([response(404, {"error": {"code": "not_found"}})])
    client = HTTPRegistryClient.from_profile(profile, transport=transport)
    assert client.get_resource("host/web") is None
    call = transport.calls[0]
    assert call["url"].endswith("/api/v1/resources/host%2Fweb")
    assert call["headers"]["CF-Access-Client-Id"] == "client-id"
    assert call["headers"]["CF-Access-Client-Secret"] == "client-secret"
    assert call["timeout_seconds"] == 20
    assert resolve_secret_reference("env:REGISTRY_CLIENT_ID") == "client-id"
    assert resolve_secret_reference(f"file:{secret}") == "client-secret"
    with pytest.raises(InputError, match="missing"):
        resolve_secret_reference(None)
    with pytest.raises(InputError, match="invalid environment"):
        resolve_secret_reference("env:bad-name")
    with pytest.raises(InputError, match="not set"):
        resolve_secret_reference("env:MISSING_HOSTCTL_SECRET")
    empty = tmp_path / "empty"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(InputError, match="empty"):
        resolve_secret_reference(f"file:{empty}")
    with pytest.raises(InputError, match="env: or file"):
        resolve_secret_reference("plain")

    assert (
        RegistryProfile(
            schema="atlas.registry-profile/v1",
            base_url="http://localhost:8787",
            access={"development_identity": "actor"},
        ).base_url
        == "http://localhost:8787"
    )
    with pytest.raises(ValidationError, match="unsupported"):
        RegistryProfile(
            schema="other",
            base_url="https://registry.example.test",
            access={"development_identity": "actor"},
        )
    with pytest.raises(ValidationError, match="HTTPS"):
        RegistryProfile(
            schema="atlas.registry-profile/v1",
            base_url="http://registry.example.test",
            access={"development_identity": "actor"},
        )
    with pytest.raises(ValidationError, match="absolute"):
        RegistryProfile(
            schema="atlas.registry-profile/v1",
            base_url="relative",
            access={"development_identity": "actor"},
        )
    with pytest.raises(ValidationError, match="credentials"):
        RegistryProfile(
            schema="atlas.registry-profile/v1",
            base_url="https://user:pass@registry.example.test?q=1",
            access={"development_identity": "actor"},
        )
    with pytest.raises(ValidationError, match="together"):
        RegistryAccess(client_id_ref="env:ID")
    with pytest.raises(ValidationError, match="exactly one"):
        RegistryAccess()
    with pytest.raises(ValidationError, match="exactly one"):
        RegistryAccess(jwt_ref="env:JWT", development_identity="actor")
    profile.write_text("invalid: true\n", encoding="utf-8")
    with pytest.raises(InputError, match="profile is invalid"):
        HTTPRegistryClient.from_profile(profile)


def test_http_client_authentication_forms_and_requirement() -> None:
    jwt_transport = ScriptedTransport([response(200, {"items": []})])
    HTTPRegistryClient(
        "https://registry.example.test",
        access_jwt="jwt",
        transport=jwt_transport,
    ).find_operation_by_idempotency_key("none")
    assert jwt_transport.calls[0]["headers"]["Cf-Access-Jwt-Assertion"] == "jwt"
    dev_transport = ScriptedTransport([response(200, {"items": []})])
    HTTPRegistryClient(
        "http://localhost:8787",
        development_identity="actor",
        transport=dev_transport,
    ).find_operation_by_idempotency_key("none")
    assert (
        dev_transport.calls[0]["headers"]["x-global-registry-dev-identity"] == "actor"
    )
    with pytest.raises(InputError, match="authentication"):
        HTTPRegistryClient("https://registry.example.test")


def test_http_resource_and_operation_reads(tmp_path: Path) -> None:
    plan = make_host_fixture(tmp_path).plan()
    operation = operation_payload(plan)
    other = operation_payload(plan, operation_id="op-other")
    other["operation"]["plan"]["intent"]["idempotencyKey"] = "other-key"
    other["resources"][0]["resourceKey"] = "host-other"
    transport = ScriptedTransport(
        [
            response(200, resource_payload(binding={"providerId": "p"})),
            response(200, operation),
            response(200, {"items": [other["operation"], operation["operation"]]}),
            response(200, operation),
            response(200, {"items": [other["operation"], operation["operation"]]}),
            response(200, other),
            response(200, operation),
        ]
    )
    client = _client(transport)
    resource = client.get_resource("host-web01")
    assert resource and resource.binding == {"providerId": "p"}
    assert client.get_operation("op-1").steps[0].name == "validate"
    assert (
        client.find_operation_by_idempotency_key(plan.metadata.idempotency_key).id
        == "op-1"
    )
    assert client.find_operation_for_resource("host-web01").id == "op-1"
    empty = _client(
        ScriptedTransport([response(200, {"items": []}), response(200, {"items": []})])
    )
    assert empty.find_operation_by_idempotency_key("none") is None
    assert empty.find_operation_for_resource("none") is None

    invalid_steps = _client(
        ScriptedTransport(
            [
                response(
                    200,
                    {
                        **operation,
                        "steps": ["not-a-step"],
                    },
                )
            ]
        )
    )
    with pytest.raises(RegistryUnavailableError, match="invalid operation"):
        invalid_steps.get_operation("op-1")


def test_http_create_operation_uses_current_route_body(tmp_path: Path) -> None:
    plan = make_host_fixture(tmp_path).plan()
    detail = operation_payload(plan)
    transport = ScriptedTransport(
        [
            response(200, resource_payload()),
            response(201, detail["operation"]),
            response(200, detail),
        ]
    )
    operation = _client(transport).create_operation(plan)
    assert operation.id == "op-1"
    call = transport.calls[1]
    assert call["method"] == "POST"
    assert call["url"].endswith("/api/v1/operations")
    assert call["body"]["intent"]["idempotencyKey"] == plan.metadata.idempotency_key
    assert call["body"]["resources"] == [
        {
            "resourceKey": "host-web01",
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
    ]
    assert call["body"]["changes"] == [
        {
            "action": "binding.replace",
            "resourceKey": "host-web01",
            "providerId": "fake-cloud",
            "providerResourceType": "compute",
        },
        {
            "action": "binding.remove",
            "resourceKey": "host-web01",
        },
    ]
    assert len(call["body"]["steps"]) == 10
    missing = _client(
        ScriptedTransport([response(404, {"error": {"code": "not_found"}})])
    )
    with pytest.raises(RegistryConflictError, match="must exist"):
        missing.create_operation(plan)

    changed = _client(ScriptedTransport([response(200, resource_payload(revision=2))]))
    with pytest.raises(RegistryConflictError, match="revision changed"):
        changed.create_operation(plan)


def test_http_lock_step_binding_lifecycle_and_operation_mutations(
    tmp_path: Path,
) -> None:
    fixture = make_host_fixture(tmp_path)
    plan = fixture.plan()
    detail = operation_payload(plan)
    running_detail = operation_payload(plan, status="running", revision=2)
    evidence = _evidence(
        plan,
        "op-1",
        ProvisioningPhase.ALLOCATE,
        StepStatus.SUCCEEDED,
        datetime.now(UTC),
    )
    provider = ProviderEvidence(
        provider="proxmox",
        resourceType="vm",
        resourceId="qemu/121",
        resourceName="web01",
        locator={"node": "pve01"},
        ownershipMarker=True,
    )
    transport = ScriptedTransport(
        [
            # acquire_locks: lease, operation detail, resource detail
            response(
                201,
                {
                    "items": [
                        {
                            "scope": "resource/host-web01",
                            "operationId": "op-1",
                            "fencingToken": 3,
                            "expiresAt": "2026-08-01T00:05:00Z",
                        }
                    ]
                },
            ),
            response(200, detail),
            response(200, resource_payload()),
            # start
            response(200, detail),
            response(200, running_detail["operation"]),
            # record step
            response(200, running_detail),
            response(200, {"id": "step-2"}),
            # reserve
            response(200, resource_payload()),
            # bind
            response(200, resource_payload()),
            response(200, {"providerId": "proxmox"}),
            # lifecycle
            response(200, resource_payload(revision=2)),
            response(
                200, resource_payload(lifecycle="allocated", revision=3)["resource"]
            ),
            # remove binding
            response(
                200, resource_payload(revision=3, binding={"providerId": "proxmox"})
            ),
            response(204),
            # complete
            response(200, running_detail),
            response(200, {**running_detail["operation"], "status": "succeeded"}),
            # fail
            response(200, running_detail),
            response(200, {**running_detail["operation"], "status": "failed"}),
            # cancel
            response(200, running_detail),
            response(200, {**running_detail["operation"], "status": "cancelled"}),
        ]
    )
    client = _client(transport)
    authority = client.acquire_locks("op-1", ["resource/host-web01"])
    assert authority.fencing_token == 3
    assert client.start_operation("op-1", authority).status == "running"
    client.record_step("op-1", evidence, authority)
    assert client.reserve_resource("op-1", plan.resource, authority).key == "host-web01"
    client.bind_provider("op-1", "host-web01", provider, authority)
    assert (
        client.update_resource_lifecycle(
            "op-1", "host-web01", "allocated", authority
        ).lifecycle_state
        == "allocated"
    )
    client.remove_provider_binding("op-1", "host-web01", authority)
    assert client.complete_operation("op-1", authority).status == "succeeded"
    assert client.fail_operation("op-1", "failed", authority).status == "failed"
    assert client.cancel_operation("op-1", authority).status == "cancelled"
    client.release_locks("op-1")
    assert transport.calls[0]["body"] == {
        "scopes": ["resource/host-web01"],
        "leaseSeconds": 300,
    }
    assert transport.calls[6]["body"]["status"] == "succeeded"
    assert transport.calls[9]["body"]["providerResourceId"] == "qemu/121"
    assert transport.calls[13]["method"] == "DELETE"


def test_http_idempotent_and_reconcile_shortcuts(tmp_path: Path) -> None:
    plan = make_host_fixture(tmp_path).plan()
    running = operation_payload(plan, status="running", step_status="succeeded")
    authority = RegistryAuthority(
        operationId="op-1",
        lockScope="resource/host-web01",
        fencingToken=1,
        operationRevision=2,
        resourceRevision=1,
    )
    evidence = _evidence(
        plan,
        "op-1",
        ProvisioningPhase.ALLOCATE,
        StepStatus.SUCCEEDED,
        datetime.now(UTC),
    )
    client = _client(
        ScriptedTransport(
            [
                response(200, running),
                response(200, running),
                response(200, running),
                response(200, running),
            ]
        )
    )
    assert client.start_operation("op-1", authority).status == "running"
    client.record_step("op-1", evidence, authority)
    assert client.mark_needs_reconcile("op-1", evidence, authority).id == "op-1"

    lifecycle_client = _client(
        ScriptedTransport([response(200, resource_payload(lifecycle="allocated"))])
    )
    assert (
        lifecycle_client.update_resource_lifecycle(
            "op-1", "host-web01", "allocated", authority
        ).lifecycle_state
        == "allocated"
    )


def test_http_rollback_keeps_terminal_allocation_evidence(tmp_path: Path) -> None:
    plan = make_host_fixture(tmp_path).plan()
    running = operation_payload(plan, status="running")
    allocate = next(step for step in running["steps"] if step["name"] == "allocate")
    allocate["status"] = "succeeded"
    authority = RegistryAuthority(
        operationId="op-1",
        lockScope="resource/host-web01",
        fencingToken=1,
        operationRevision=2,
        resourceRevision=1,
    )
    rolled_back = _evidence(
        plan,
        "op-1",
        ProvisioningPhase.ALLOCATE,
        StepStatus.ROLLED_BACK,
        datetime.now(UTC),
    )
    transport = ScriptedTransport([response(200, running)])
    _client(transport).record_step("op-1", rolled_back, authority)
    assert len(transport.calls) == 1


def test_http_renew_locks_recovers_scope_from_operation(tmp_path: Path) -> None:
    plan = make_host_fixture(tmp_path).plan()
    detail = operation_payload(plan, status="running")
    transport = ScriptedTransport(
        [
            response(200, detail),
            response(
                201,
                {
                    "items": [
                        {
                            "scope": "resource/host-web01",
                            "fencingToken": 4,
                        }
                    ]
                },
            ),
            response(200, detail),
            response(200, resource_payload()),
        ]
    )
    assert _client(transport).renew_locks("op-1").fencing_token == 4
    broken = _client(
        ScriptedTransport([response(404, {"error": {"code": "not_found"}})])
    )
    with pytest.raises(RegistryConflictError, match="scopes"):
        broken.renew_locks("missing")


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, RegistryAuthenticationError),
        (403, RegistryAuthenticationError),
        (409, RegistryConflictError),
        (500, RegistryError),
    ],
)
def test_http_errors_are_typed_and_redacted(
    status: int, error: type[Exception]
) -> None:
    transport = ScriptedTransport(
        [
            response(
                status,
                {
                    "error": {
                        "code": "revision_conflict",
                        "message": "token=do-not-leak",
                    }
                },
            )
        ]
    )
    with pytest.raises(error) as raised:
        _client(transport).get_operation("op-1")
    assert "do-not-leak" not in str(raised.value)


def test_http_invalid_responses_and_transport_failures() -> None:
    for body in (b"not-json", json.dumps([]).encode()):
        client = _client(
            ScriptedTransport([type("R", (), {"status": 200, "body": body})()])
        )
        with pytest.raises(RegistryUnavailableError):
            client.get_operation("op-1")
    with pytest.raises(RegistryUnavailableError, match="request failed"):
        _client(ScriptedTransport([TimeoutError()])).get_operation("op-1")
    with pytest.raises(RegistryUnavailableError, match="invalid resource"):
        _client(ScriptedTransport([response(200, {"resource": []})])).get_resource("r")
    with pytest.raises(RegistryUnavailableError, match="invalid Resource"):
        _client(ScriptedTransport([response(200, {"bad": True})])).get_resource("r")
    with pytest.raises(RegistryUnavailableError, match="invalid provider Binding"):
        _client(
            ScriptedTransport(
                [response(200, resource_payload(binding=["invalid"]))]
            )
        ).get_resource("r")
    with pytest.raises(RegistryUnavailableError, match="operation list"):
        _client(
            ScriptedTransport([response(200, {"items": {}})])
        ).find_operation_by_idempotency_key("x")
    with pytest.raises(RegistryUnavailableError, match="invalid operation"):
        _client(
            ScriptedTransport(
                [
                    response(
                        200,
                        {
                            "operation": {
                                "id": "op-1",
                                "status": "planned",
                                "revision": 1,
                            },
                            "resources": ["invalid"],
                            "steps": [],
                        },
                    )
                ]
            )
        ).get_operation("op-1")
    with pytest.raises(RegistryUnavailableError, match="lock leases"):
        _client(ScriptedTransport([response(201, {"items": []})])).acquire_locks(
            "op", ["resource/r"]
        )


@pytest.mark.parametrize(
    "lease",
    [
        {"scope": "resource/other", "fencingToken": 1},
        {"scope": "resource/r", "fencingToken": True},
        {
            "scope": "resource/r",
            "operationId": "op-other",
            "fencingToken": 1,
        },
    ],
)
def test_http_rejects_mismatched_lock_lease(lease: dict[str, object]) -> None:
    client = _client(
        ScriptedTransport([response(201, {"items": [lease]})])
    )
    with pytest.raises(RegistryUnavailableError, match="invalid lock lease"):
        client.acquire_locks("op-1", ["resource/r"])


def test_http_binding_is_idempotent_and_refuses_replacement(tmp_path: Path) -> None:
    plan = make_host_fixture(tmp_path).plan()
    authority = RegistryAuthority(
        operationId="op-1",
        lockScope="resource/host-web01",
        fencingToken=1,
        operationRevision=1,
        resourceRevision=1,
    )
    provider = ProviderEvidence(
        provider="proxmox",
        resourceType="proxmox.qemu",
        resourceId="qemu/121",
        resourceName="web01",
        locator={"node": "pve01", "vmid": 121},
        ownershipMarker=True,
    )
    binding = {
        "providerId": provider.provider,
        "providerResourceType": provider.resource_type,
        "providerResourceId": provider.resource_id,
        "providerResourceName": provider.resource_name,
        "locator": provider.locator,
    }
    idempotent_transport = ScriptedTransport(
        [response(200, resource_payload(binding=binding))]
    )
    _client(idempotent_transport).bind_provider(
        "op-1",
        plan.resource.id,
        provider,
        authority,
    )
    assert len(idempotent_transport.calls) == 1

    different = {**binding, "providerResourceId": "qemu/999"}
    with pytest.raises(RegistryConflictError, match="different provider Binding"):
        _client(
            ScriptedTransport([response(200, resource_payload(binding=different))])
        ).bind_provider("op-1", plan.resource.id, provider, authority)


@pytest.mark.parametrize(
    "authority",
    [
        RegistryAuthority(
            operationId="op-other",
            lockScope="resource/host-web01",
            fencingToken=1,
            operationRevision=1,
            resourceRevision=1,
        ),
        RegistryAuthority(
            operationId="op-1",
            lockScope="resource/other",
            fencingToken=1,
            operationRevision=1,
            resourceRevision=1,
        ),
    ],
)
def test_http_resource_mutation_requires_matching_authority(
    tmp_path: Path,
    authority: RegistryAuthority,
) -> None:
    plan = make_host_fixture(tmp_path).plan()
    with pytest.raises(RegistryConflictError, match="does not cover"):
        _client(ScriptedTransport([])).reserve_resource(
            "op-1",
            plan.resource,
            authority,
        )


def test_in_memory_registry_conflicts_transitions_and_fencing(tmp_path: Path) -> None:
    fixture = make_host_fixture(tmp_path)
    plan = fixture.plan()
    registry = InMemoryRegistryClient(list(fixture.registry.resources.values()))
    operation = registry.create_operation(plan)
    assert registry.create_operation(plan).id == operation.id
    assert registry.find_operation_for_resource("host-web01").id == operation.id
    authority = registry.acquire_locks(operation.id, ["resource/host-web01"])
    registry.start_operation(operation.id, authority)
    reserved = registry.reserve_resource(operation.id, plan.resource, authority)
    assert reserved.lifecycle_state == "provisioning"
    registry.reserve_resource(operation.id, plan.resource, authority)
    provider = ProviderEvidence(
        provider="fake",
        resourceType="compute",
        resourceId="fake/1",
        resourceName="web01",
        locator={},
        ownershipMarker=True,
    )
    registry.bind_provider(operation.id, plan.resource.id, provider, authority)
    revision = registry.get_resource(plan.resource.id).revision
    registry.bind_provider(operation.id, plan.resource.id, provider, authority)
    assert registry.get_resource(plan.resource.id).revision == revision
    with pytest.raises(RegistryConflictError, match="different provider Binding"):
        registry.bind_provider(
            operation.id,
            plan.resource.id,
            provider.model_copy(update={"resource_id": "fake/other"}),
            authority,
        )
    registry.remove_provider_binding(operation.id, plan.resource.id, authority)
    registry.update_resource_lifecycle(
        operation.id, plan.resource.id, "allocated", authority
    )
    registry.update_resource_lifecycle(
        operation.id, plan.resource.id, "ready", authority
    )
    assert registry.get_resource(plan.resource.id).lifecycle_state == "active"
    renewed = registry.renew_locks(operation.id)
    with pytest.raises(RegistryConflictError, match="stale"):
        registry.start_operation(operation.id, authority)
    registry.release_locks(operation.id)
    assert renewed.fencing_token > authority.fencing_token

    with pytest.raises(RegistryConflictError, match="missing"):
        InMemoryRegistryClient().create_operation(plan)
    changed = InMemoryRegistryClient(
        [fixture.registry.resources["host-web01"].model_copy(update={"revision": 2})]
    )
    with pytest.raises(RegistryConflictError, match="revision changed"):
        changed.create_operation(plan)
    wrong = InMemoryRegistryClient(
        [
            RegistryResource(
                id="r",
                key="host-web01",
                kind="compute",
                name="other",
                lifecycleState="absent",
                revision=1,
            )
        ]
    )
    wrong_op = wrong.create_operation(plan)
    wrong_auth = wrong.acquire_locks(wrong_op.id, ["resource/host-web01"])
    with pytest.raises(RegistryConflictError, match="name collision"):
        wrong.reserve_resource(wrong_op.id, plan.resource, wrong_auth)


def test_load_registry_client_uses_plan_profile(tmp_path: Path, monkeypatch) -> None:
    fixture = make_host_fixture(tmp_path)
    plan = fixture.plan()
    sentinel = object()
    monkeypatch.setattr(
        "atlas_host_operations.registry.HTTPRegistryClient.from_profile",
        lambda path: sentinel,
    )
    assert load_registry_client(plan) is sentinel


def test_urllib_transport_wraps_http_error(monkeypatch) -> None:
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        @staticmethod
        def read() -> bytes:
            return b"{}"

    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: Response())
    assert (
        UrllibTransport().request("GET", "https://example.test", {}, None, 1).status
        == 200
    )
