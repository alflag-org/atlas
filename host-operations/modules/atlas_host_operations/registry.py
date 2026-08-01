"""Global Registry client contract, current HTTP adapter, and in-memory fake."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urljoin, urlparse
from uuid import uuid4

from pydantic import Field, ValidationError, model_validator

from atlas_host_operations.artifacts import read_yaml, safe_file
from atlas_host_operations.errors import (
    InputError,
    RegistryAuthenticationError,
    RegistryConflictError,
    RegistryError,
    RegistryUnavailableError,
)
from atlas_host_operations.lifecycle import (
    OperationStatus,
    ProvisioningPhase,
    ResourceLifecycle,
    StepStatus,
    validate_operation_transition,
    validate_step_transition,
)
from atlas_host_operations.models import (
    HostModel,
    HostOperationEvidence,
    HostOperationPlan,
    HostPlanResource,
    ProviderEvidence,
    RegistryAuthority,
    RegistryOperation,
    RegistryResource,
    RegistryStep,
)


class RegistryClient(Protocol):
    def get_resource(self, resource_id: str) -> RegistryResource | None: ...

    def get_operation(self, operation_id: str) -> RegistryOperation | None: ...

    def find_operation_by_idempotency_key(
        self, key: str
    ) -> RegistryOperation | None: ...

    def find_operation_for_resource(
        self, resource_id: str
    ) -> RegistryOperation | None: ...

    def create_operation(self, plan: HostOperationPlan) -> RegistryOperation: ...

    def acquire_locks(
        self, operation_id: str, scopes: list[str]
    ) -> RegistryAuthority: ...

    def renew_locks(self, operation_id: str) -> RegistryAuthority: ...

    def start_operation(
        self,
        operation_id: str,
        authority: RegistryAuthority,
    ) -> RegistryOperation: ...

    def record_step(
        self,
        operation_id: str,
        step: HostOperationEvidence,
        authority: RegistryAuthority,
    ) -> None: ...

    def reserve_resource(
        self,
        operation_id: str,
        resource: HostPlanResource,
        authority: RegistryAuthority,
    ) -> RegistryResource: ...

    def bind_provider(
        self,
        operation_id: str,
        resource_id: str,
        binding: ProviderEvidence,
        authority: RegistryAuthority,
    ) -> None: ...

    def remove_provider_binding(
        self,
        operation_id: str,
        resource_id: str,
        authority: RegistryAuthority,
    ) -> None: ...

    def update_resource_lifecycle(
        self,
        operation_id: str,
        resource_id: str,
        state: str,
        authority: RegistryAuthority,
    ) -> RegistryResource: ...

    def complete_operation(
        self,
        operation_id: str,
        authority: RegistryAuthority,
    ) -> RegistryOperation: ...

    def fail_operation(
        self,
        operation_id: str,
        error: str,
        authority: RegistryAuthority,
    ) -> RegistryOperation: ...

    def mark_needs_reconcile(
        self,
        operation_id: str,
        evidence: HostOperationEvidence,
        authority: RegistryAuthority,
    ) -> RegistryOperation: ...

    def cancel_operation(
        self,
        operation_id: str,
        authority: RegistryAuthority,
    ) -> RegistryOperation: ...

    def release_locks(self, operation_id: str) -> None: ...


class RegistryAccess(HostModel):
    client_id_ref: str | None = None
    client_secret_ref: str | None = None
    jwt_ref: str | None = None
    development_identity: str | None = None

    @model_validator(mode="after")
    def require_one_authentication_form(self) -> RegistryAccess:
        service_pair = (
            self.client_id_ref is not None or self.client_secret_ref is not None
        )
        if service_pair and not (self.client_id_ref and self.client_secret_ref):
            raise ValueError(
                "client_id_ref and client_secret_ref must be supplied together"
            )
        forms = sum(
            (
                bool(self.client_id_ref and self.client_secret_ref),
                self.jwt_ref is not None,
                self.development_identity is not None,
            )
        )
        if forms != 1:
            raise ValueError("exactly one Registry authentication form is required")
        return self


class RegistryProfile(HostModel):
    schema_version: str = Field(alias="schema")
    base_url: str
    timeout_seconds: int = Field(default=30, gt=0, le=300)
    access: RegistryAccess

    @model_validator(mode="after")
    def validate_profile(self) -> RegistryProfile:
        if self.schema_version != "atlas.registry-profile/v1":
            raise ValueError("unsupported Registry profile schema")
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "base_url must not include credentials, query, or fragment"
            )
        if parsed.scheme != "https" and parsed.hostname not in {
            "localhost",
            "127.0.0.1",
        }:
            raise ValueError("base_url must use HTTPS outside localhost")
        self.base_url = self.base_url.rstrip("/")
        return self


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    body: bytes


class HTTPTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: int,
    ) -> HTTPResponse: ...


class UrllibTransport:
    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: int,
    ) -> HTTPResponse:
        request = urllib.request.Request(
            url,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                return HTTPResponse(response.status, response.read())
        except urllib.error.HTTPError as exc:
            return HTTPResponse(exc.code, exc.read())


class HTTPRegistryClient:
    """Map the host contract to Global Registry's current `/api/v1` routes."""

    def __init__(
        self,
        base_url: str,
        *,
        access_client_id: str | None = None,
        access_client_secret: str | None = None,
        access_jwt: str | None = None,
        development_identity: str | None = None,
        timeout_seconds: int = 30,
        transport: HTTPTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/") + "/"
        self._timeout_seconds = timeout_seconds
        self._transport = transport or UrllibTransport()
        self._auth_headers: dict[str, str] = {}
        if access_client_id is not None and access_client_secret is not None:
            self._auth_headers = {
                "CF-Access-Client-Id": access_client_id,
                "CF-Access-Client-Secret": access_client_secret,
            }
        elif access_jwt is not None:
            self._auth_headers = {"Cf-Access-Jwt-Assertion": access_jwt}
        elif development_identity is not None:
            self._auth_headers = {
                "x-global-registry-dev-identity": development_identity
            }
        else:
            raise InputError("Registry authentication is required")
        self._lock_scopes: dict[str, list[str]] = {}

    @classmethod
    def from_profile(
        cls,
        path: str | Path,
        *,
        transport: HTTPTransport | None = None,
    ) -> HTTPRegistryClient:
        source = safe_file(path)
        try:
            profile = RegistryProfile.model_validate(read_yaml(source))
        except ValidationError as exc:
            raise InputError(f"Registry profile is invalid: {exc}") from exc
        access = profile.access
        return cls(
            profile.base_url,
            access_client_id=(
                resolve_secret_reference(access.client_id_ref)
                if access.client_id_ref is not None
                else None
            ),
            access_client_secret=(
                resolve_secret_reference(access.client_secret_ref)
                if access.client_secret_ref is not None
                else None
            ),
            access_jwt=(
                resolve_secret_reference(access.jwt_ref)
                if access.jwt_ref is not None
                else None
            ),
            development_identity=access.development_identity,
            timeout_seconds=profile.timeout_seconds,
            transport=transport,
        )

    def get_resource(self, resource_id: str) -> RegistryResource | None:
        payload = self._request(
            "GET",
            f"api/v1/resources/{_segment(resource_id)}",
            allow_not_found=True,
        )
        if payload is None:
            return None
        raw = payload.get("resource", payload)
        if not isinstance(raw, dict):
            raise RegistryUnavailableError(
                "Global Registry returned an invalid resource"
            )
        binding = payload.get("binding")
        if binding is not None and not isinstance(binding, dict):
            raise RegistryUnavailableError(
                "Global Registry returned an invalid provider Binding"
            )
        return _resource_model(raw, binding)

    def get_operation(self, operation_id: str) -> RegistryOperation | None:
        payload = self._request(
            "GET",
            f"api/v1/operations/{_segment(operation_id)}",
            allow_not_found=True,
        )
        if payload is None:
            return None
        return _operation_model(payload)

    def find_operation_by_idempotency_key(self, key: str) -> RegistryOperation | None:
        payload = self._request("GET", "api/v1/operations")
        items = payload.get("items")
        if not isinstance(items, list):
            raise RegistryUnavailableError(
                "Global Registry returned an invalid operation list"
            )
        for item in items:
            if not isinstance(item, dict):
                raise RegistryUnavailableError(
                    "Global Registry returned an invalid operation list"
                )
            listed = _operation_model(item)
            plan = listed.plan
            intent = plan.get("intent") if isinstance(plan, dict) else None
            if isinstance(intent, dict) and intent.get("idempotencyKey") == key:
                return self.get_operation(listed.id)
        return None

    def find_operation_for_resource(self, resource_id: str) -> RegistryOperation | None:
        payload = self._request("GET", "api/v1/operations")
        items = payload.get("items")
        if not isinstance(items, list):
            raise RegistryUnavailableError(
                "Global Registry returned an invalid operation list"
            )
        for item in items:
            if not isinstance(item, dict):
                raise RegistryUnavailableError(
                    "Global Registry returned an invalid operation list"
                )
            listed = _operation_model(item)
            operation = self.get_operation(listed.id)
            if operation and any(
                resource.get("resourceKey") == resource_id
                for resource in operation.resources
            ):
                return operation
        return None

    def create_operation(self, plan: HostOperationPlan) -> RegistryOperation:
        resource = self.get_resource(plan.resource.id)
        if resource is None:
            raise RegistryConflictError(
                "Resource identity must exist in Global Registry before hostctl apply"
            )
        if resource.revision != plan.resource.registry_revision:
            raise RegistryConflictError("Resource revision changed after hostctl plan")
        resource_plan = _host_resource_plan(resource)
        changes = _host_binding_changes(plan, resource.key)
        body = {
            "kind": "host_create",
            "intent": {
                "idempotencyKey": plan.metadata.idempotency_key,
                "planId": plan.metadata.plan_id,
                "planFingerprint": plan.metadata.fingerprint,
                "hostPlan": plan.as_artifact(),
            },
            "destructive": False,
            "resources": [resource_plan],
            "changes": changes,
            "steps": [
                {
                    "position": position,
                    "name": phase.value,
                    "gate": {"lockRequired": phase is not ProvisioningPhase.VALIDATE},
                }
                for position, phase in enumerate(plan.phases)
            ],
        }
        created = self._request("POST", "api/v1/operations", body=body, expected={201})
        operation_id = created.get("id")
        if not isinstance(operation_id, str):
            raise RegistryUnavailableError(
                "Global Registry returned an invalid operation"
            )
        operation = self.get_operation(operation_id)
        if operation is None:
            raise RegistryUnavailableError("created operation could not be read back")
        return operation

    def acquire_locks(self, operation_id: str, scopes: list[str]) -> RegistryAuthority:
        response = self._request(
            "POST",
            f"api/v1/operations/{_segment(operation_id)}/locks",
            body={"scopes": scopes, "leaseSeconds": 300},
            expected={201},
        )
        items = response.get("items")
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            raise RegistryUnavailableError(
                "Global Registry returned invalid lock leases"
            )
        lease = items[0]
        scope = lease.get("scope")
        token = lease.get("fencingToken")
        lease_operation = lease.get("operationId")
        if (
            not isinstance(scope, str)
            or scope not in scopes
            or not isinstance(token, int)
            or isinstance(token, bool)
            or (lease_operation is not None and lease_operation != operation_id)
        ):
            raise RegistryUnavailableError(
                "Global Registry returned an invalid lock lease"
            )
        self._lock_scopes[operation_id] = list(scopes)
        return self._authority(operation_id, scope, token)

    def renew_locks(self, operation_id: str) -> RegistryAuthority:
        scopes = self._lock_scopes.get(operation_id)
        if not scopes:
            operation = self.get_operation(operation_id)
            if operation is None or not operation.resources:
                raise RegistryConflictError("operation lock scopes are unavailable")
            resource_key = operation.resources[0].get("resourceKey")
            if not isinstance(resource_key, str):
                raise RegistryConflictError("operation resource scope is unavailable")
            scopes = [f"resource/{resource_key}"]
        return self.acquire_locks(operation_id, scopes)

    def start_operation(
        self,
        operation_id: str,
        authority: RegistryAuthority,
    ) -> RegistryOperation:
        operation = self._required_operation(operation_id)
        if operation.status == "running":
            return operation
        payload = self._mutation_body(operation.revision, authority)
        updated = self._request(
            "POST",
            f"api/v1/operations/{_segment(operation_id)}/start",
            body=payload,
        )
        return _operation_model({"operation": updated, "resources": [], "steps": []})

    def record_step(
        self,
        operation_id: str,
        step: HostOperationEvidence,
        authority: RegistryAuthority,
    ) -> None:
        operation = self._required_operation(operation_id)
        registry_step = next(
            (
                candidate
                for candidate in operation.steps
                if candidate.name == step.phase.value
            ),
            None,
        )
        if registry_step is None:
            raise RegistryConflictError(
                f"operation step is missing: {step.phase.value}"
            )
        status = _registry_step_status(step.status)
        if registry_step.status == "succeeded" and step.status in {
            StepStatus.SUCCEEDED,
            StepStatus.ROLLED_BACK,
        }:
            return
        self._request(
            "PATCH",
            (
                f"api/v1/operations/{_segment(operation_id)}/steps/"
                f"{_segment(registry_step.id)}"
            ),
            body={
                **self._mutation_body(registry_step.revision, authority),
                "status": status,
                "evidence": step.as_artifact(),
            },
        )

    def reserve_resource(
        self,
        operation_id: str,
        resource: HostPlanResource,
        authority: RegistryAuthority,
    ) -> RegistryResource:
        _require_resource_authority(operation_id, resource.id, authority)
        current = self.get_resource(resource.id)
        if current is None:
            raise RegistryConflictError("planned Resource identity is missing")
        if current.name != resource.name:
            raise RegistryConflictError("planned Resource name does not match Registry")
        if (
            current.lifecycle_state == "absent"
            and current.revision != resource.registry_revision
        ):
            raise RegistryConflictError("Resource revision changed after hostctl plan")
        if current.lifecycle_state not in {
            "absent",
            "allocated",
            "bootstrapped",
            "configured",
            "ready",
        }:
            raise RegistryConflictError(
                f"Resource lifecycle cannot be provisioned: {current.lifecycle_state}"
            )
        return current

    def bind_provider(
        self,
        operation_id: str,
        resource_id: str,
        binding: ProviderEvidence,
        authority: RegistryAuthority,
    ) -> None:
        _require_resource_authority(operation_id, resource_id, authority)
        resource = self._required_resource(resource_id)
        if resource.binding is not None:
            if provider_binding_matches(resource.binding, binding):
                return
            raise RegistryConflictError(
                "Resource has a different provider Binding"
            )
        self._request(
            "PUT",
            f"api/v1/resources/{_segment(resource_id)}/binding",
            body={
                "providerId": binding.provider,
                "providerResourceType": binding.resource_type,
                "providerResourceId": binding.resource_id,
                "providerResourceName": binding.resource_name,
                "locator": binding.locator,
                "expectedRevision": resource.revision,
                "operationId": operation_id,
                "fencingToken": authority.fencing_token,
            },
        )

    def remove_provider_binding(
        self,
        operation_id: str,
        resource_id: str,
        authority: RegistryAuthority,
    ) -> None:
        _require_resource_authority(operation_id, resource_id, authority)
        resource = self._required_resource(resource_id)
        self._request(
            "DELETE",
            f"api/v1/resources/{_segment(resource_id)}/binding",
            body={
                "expectedRevision": resource.revision,
                "operationId": operation_id,
                "fencingToken": authority.fencing_token,
            },
            expected={204},
        )

    def update_resource_lifecycle(
        self,
        operation_id: str,
        resource_id: str,
        state: str,
        authority: RegistryAuthority,
    ) -> RegistryResource:
        _require_resource_authority(operation_id, resource_id, authority)
        resource = self._required_resource(resource_id)
        if resource.lifecycle_state == state:
            return resource
        payload = self._request(
            "POST",
            f"api/v1/resources/{_segment(resource_id)}/transitions",
            body={
                "targetState": state,
                "expectedRevision": resource.revision,
                "operationId": operation_id,
                "fencingToken": authority.fencing_token,
            },
        )
        return _resource_model(payload)

    def complete_operation(
        self,
        operation_id: str,
        authority: RegistryAuthority,
    ) -> RegistryOperation:
        return self._operation_mutation(operation_id, "complete", authority)

    def fail_operation(
        self,
        operation_id: str,
        error: str,
        authority: RegistryAuthority,
    ) -> RegistryOperation:
        return self._operation_mutation(operation_id, "fail", authority)

    def mark_needs_reconcile(
        self,
        operation_id: str,
        evidence: HostOperationEvidence,
        authority: RegistryAuthority,
    ) -> RegistryOperation:
        self.record_step(operation_id, evidence, authority)
        return self._required_operation(operation_id)

    def cancel_operation(
        self,
        operation_id: str,
        authority: RegistryAuthority,
    ) -> RegistryOperation:
        return self._operation_mutation(operation_id, "cancel", authority)

    def release_locks(self, operation_id: str) -> None:
        # The current API has leased locks but no explicit release route. The lease
        # expires server-side; do not invent a non-existent endpoint.
        self._lock_scopes.pop(operation_id, None)

    def _operation_mutation(
        self,
        operation_id: str,
        action: str,
        authority: RegistryAuthority,
    ) -> RegistryOperation:
        operation = self._required_operation(operation_id)
        payload = self._request(
            "POST",
            f"api/v1/operations/{_segment(operation_id)}/{action}",
            body=self._mutation_body(operation.revision, authority),
        )
        return _operation_model({"operation": payload, "resources": [], "steps": []})

    def _authority(
        self, operation_id: str, scope: str, token: int
    ) -> RegistryAuthority:
        operation = self._required_operation(operation_id)
        if not operation.resources:
            raise RegistryConflictError("operation has no Resource")
        resource_key = operation.resources[0].get("resourceKey")
        if not isinstance(resource_key, str):
            raise RegistryUnavailableError("operation Resource is invalid")
        resource = self._required_resource(resource_key)
        return RegistryAuthority(
            operationId=operation_id,
            lockScope=scope,
            fencingToken=token,
            operationRevision=operation.revision,
            resourceRevision=resource.revision,
        )

    @staticmethod
    def _mutation_body(
        expected_revision: int,
        authority: RegistryAuthority,
    ) -> dict[str, Any]:
        return {
            "expectedRevision": expected_revision,
            "lockScope": authority.lock_scope,
            "fencingToken": authority.fencing_token,
        }

    def _required_operation(self, operation_id: str) -> RegistryOperation:
        operation = self.get_operation(operation_id)
        if operation is None:
            raise RegistryConflictError(f"operation not found: {operation_id}")
        return operation

    def _required_resource(self, resource_id: str) -> RegistryResource:
        resource = self.get_resource(resource_id)
        if resource is None:
            raise RegistryConflictError(f"Resource not found: {resource_id}")
        return resource

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        expected: set[int] | None = None,
        allow_not_found: bool = False,
    ) -> dict[str, Any] | None:
        headers = {"Accept": "application/json", **self._auth_headers}
        encoded = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            encoded = json.dumps(
                body, ensure_ascii=False, separators=(",", ":")
            ).encode()
        url = urljoin(self._base_url, path)
        try:
            response = self._transport.request(
                method,
                url,
                headers,
                encoded,
                self._timeout_seconds,
            )
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            raise RegistryUnavailableError("Global Registry request failed") from exc
        expected_status = expected or {200}
        if allow_not_found and response.status == 404:
            return None
        if response.status not in expected_status:
            self._raise_http_error(response)
        if response.status == 204:
            return {}
        try:
            payload = json.loads(response.body)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise RegistryUnavailableError(
                "Global Registry returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise RegistryUnavailableError(
                "Global Registry returned a non-object response"
            )
        return payload

    @staticmethod
    def _raise_http_error(response: HTTPResponse) -> None:
        code = "unknown"
        try:
            payload = json.loads(response.body)
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict) and isinstance(error.get("code"), str):
                code = error["code"]
        except (json.JSONDecodeError, UnicodeError):
            pass
        message = f"Global Registry returned HTTP {response.status} ({code})"
        if response.status in {401, 403}:
            raise RegistryAuthenticationError(message)
        if response.status == 409:
            raise RegistryConflictError(message)
        raise RegistryError(message)


class InMemoryRegistryClient:
    """Durable-state fake with fencing and transition checks."""

    def __init__(self, resources: list[RegistryResource] | None = None) -> None:
        self.resources = {resource.key: resource for resource in resources or []}
        self.operations: dict[str, RegistryOperation] = {}
        self.call_log: list[str] = []
        self._idempotency: dict[str, str] = {}
        self._tokens: dict[str, int] = {}
        self._scopes: dict[str, list[str]] = {}

    def get_resource(self, resource_id: str) -> RegistryResource | None:
        self.call_log.append("get-resource")
        return self.resources.get(resource_id)

    def get_operation(self, operation_id: str) -> RegistryOperation | None:
        self.call_log.append("get-operation")
        return self.operations.get(operation_id)

    def find_operation_by_idempotency_key(self, key: str) -> RegistryOperation | None:
        self.call_log.append("find-operation")
        operation_id = self._idempotency.get(key)
        return self.operations.get(operation_id) if operation_id else None

    def find_operation_for_resource(self, resource_id: str) -> RegistryOperation | None:
        self.call_log.append("find-resource-operation")
        for operation in reversed(list(self.operations.values())):
            if any(
                item.get("resourceKey") == resource_id for item in operation.resources
            ):
                return operation
        return None

    def create_operation(self, plan: HostOperationPlan) -> RegistryOperation:
        self.call_log.append("create-operation")
        existing = self.find_operation_by_idempotency_key(plan.metadata.idempotency_key)
        if existing is not None:
            return existing
        resource = self.resources.get(plan.resource.id)
        if resource is None:
            raise RegistryConflictError("planned Resource identity is missing")
        if resource.revision != plan.resource.registry_revision:
            raise RegistryConflictError("Resource revision changed after hostctl plan")
        operation_id = f"op-{uuid4()}"
        resource_plan = _host_resource_plan(resource)
        changes = _host_binding_changes(plan, resource.key)
        operation = RegistryOperation(
            id=operation_id,
            status=OperationStatus.PLANNED.value,
            revision=1,
            plan={
                "kind": "host_create",
                "intent": {
                    "idempotencyKey": plan.metadata.idempotency_key,
                    "planId": plan.metadata.plan_id,
                    "planFingerprint": plan.metadata.fingerprint,
                    "hostPlan": plan.as_artifact(),
                },
                "resources": [resource_plan],
                "changes": changes,
            },
            resources=[resource_plan],
            steps=[
                RegistryStep(
                    id=f"step-{position}",
                    name=phase.value,
                    status=StepStatus.PENDING.value,
                    revision=1,
                )
                for position, phase in enumerate(plan.phases)
            ],
        )
        self.operations[operation_id] = operation
        self._idempotency[plan.metadata.idempotency_key] = operation_id
        return operation

    def acquire_locks(self, operation_id: str, scopes: list[str]) -> RegistryAuthority:
        self.call_log.append("acquire-locks")
        operation = self._required_operation(operation_id)
        status = OperationStatus(operation.status)
        if status not in {
            OperationStatus.PLANNED,
            OperationStatus.LOCKED,
            OperationStatus.RUNNING,
            OperationStatus.NEEDS_RECONCILE,
        }:
            raise RegistryConflictError("operation is not lockable")
        token = self._tokens.get(scopes[0], 0) + 1
        self._tokens[scopes[0]] = token
        self._scopes[operation_id] = list(scopes)
        if status is OperationStatus.PLANNED:
            operation = self._replace_operation(
                operation,
                status=OperationStatus.LOCKED.value,
            )
        resource_key = operation.resources[0]["resourceKey"]
        resource = self.resources[str(resource_key)]
        return RegistryAuthority(
            operationId=operation_id,
            lockScope=scopes[0],
            fencingToken=token,
            operationRevision=operation.revision,
            resourceRevision=resource.revision,
        )

    def renew_locks(self, operation_id: str) -> RegistryAuthority:
        self.call_log.append("renew-locks")
        scopes = self._scopes.get(operation_id)
        if not scopes:
            raise RegistryConflictError("operation has no known lock scopes")
        return self.acquire_locks(operation_id, scopes)

    def start_operation(
        self,
        operation_id: str,
        authority: RegistryAuthority,
    ) -> RegistryOperation:
        self.call_log.append("start-operation")
        self._check_authority(operation_id, authority)
        operation = self._required_operation(operation_id)
        current = OperationStatus(operation.status)
        if current is OperationStatus.RUNNING:
            return operation
        validate_operation_transition(current, OperationStatus.RUNNING)
        return self._replace_operation(operation, status=OperationStatus.RUNNING.value)

    def record_step(
        self,
        operation_id: str,
        step: HostOperationEvidence,
        authority: RegistryAuthority,
    ) -> None:
        self.call_log.append(f"record-step:{step.phase.value}:{step.status.value}")
        self._check_authority(operation_id, authority)
        operation = self._required_operation(operation_id)
        updated: list[RegistryStep] = []
        found = False
        for current in operation.steps:
            if current.name != step.phase.value:
                updated.append(current)
                continue
            found = True
            if (
                current.status == StepStatus.SUCCEEDED.value
                and step.status is StepStatus.SUCCEEDED
            ):
                updated.append(current)
                continue
            validate_step_transition(StepStatus(current.status), step.status)
            updated.append(
                RegistryStep(
                    id=current.id,
                    name=current.name,
                    status=step.status.value,
                    evidence=step.as_artifact(),
                    revision=current.revision + 1,
                )
            )
        if not found:
            raise RegistryConflictError(
                f"operation step is missing: {step.phase.value}"
            )
        self.operations[operation_id] = operation.model_copy(update={"steps": updated})

    def reserve_resource(
        self,
        operation_id: str,
        resource: HostPlanResource,
        authority: RegistryAuthority,
    ) -> RegistryResource:
        self.call_log.append("reserve-resource")
        self._check_authority(operation_id, authority)
        current = self._required_resource(resource.id)
        if current.name != resource.name:
            raise RegistryConflictError("Resource name collision")
        if (
            current.lifecycle_state == ResourceLifecycle.ABSENT.value
            and current.revision != resource.registry_revision
        ):
            raise RegistryConflictError("Resource revision changed after hostctl plan")
        if current.lifecycle_state == ResourceLifecycle.ABSENT.value:
            current = current.model_copy(
                update={
                    "lifecycle_state": ResourceLifecycle.PROVISIONING.value,
                    "revision": current.revision + 1,
                }
            )
            self.resources[current.key] = current
        elif current.lifecycle_state not in {
            ResourceLifecycle.PROVISIONING.value,
            ResourceLifecycle.ACTIVE.value,
        }:
            raise RegistryConflictError("Resource lifecycle collision")
        return current

    def bind_provider(
        self,
        operation_id: str,
        resource_id: str,
        binding: ProviderEvidence,
        authority: RegistryAuthority,
    ) -> None:
        self.call_log.append("bind-provider")
        self._check_authority(operation_id, authority)
        resource = self._required_resource(resource_id)
        if resource.binding is not None:
            if provider_binding_matches(resource.binding, binding):
                return
            raise RegistryConflictError(
                "Resource has a different provider Binding"
            )
        self.resources[resource_id] = resource.model_copy(
            update={
                "binding": binding.model_dump(mode="json", by_alias=True),
                "revision": resource.revision + 1,
            }
        )

    def remove_provider_binding(
        self,
        operation_id: str,
        resource_id: str,
        authority: RegistryAuthority,
    ) -> None:
        self.call_log.append("remove-binding")
        self._check_authority(operation_id, authority)
        resource = self._required_resource(resource_id)
        self.resources[resource_id] = resource.model_copy(
            update={"binding": None, "revision": resource.revision + 1}
        )

    def update_resource_lifecycle(
        self,
        operation_id: str,
        resource_id: str,
        state: str,
        authority: RegistryAuthority,
    ) -> RegistryResource:
        self.call_log.append(f"lifecycle:{state}")
        self._check_authority(operation_id, authority)
        resource = self._required_resource(resource_id)
        mapped = (
            ResourceLifecycle.ACTIVE.value
            if state in {"ready", ResourceLifecycle.ACTIVE.value}
            else ResourceLifecycle.PROVISIONING.value
        )
        if resource.lifecycle_state != mapped:
            resource = resource.model_copy(
                update={"lifecycle_state": mapped, "revision": resource.revision + 1}
            )
            self.resources[resource_id] = resource
        return resource

    def complete_operation(
        self,
        operation_id: str,
        authority: RegistryAuthority,
    ) -> RegistryOperation:
        self.call_log.append("complete-operation")
        return self._transition_operation(
            operation_id,
            OperationStatus.COMPLETED,
            authority,
        )

    def fail_operation(
        self,
        operation_id: str,
        error: str,
        authority: RegistryAuthority,
    ) -> RegistryOperation:
        self.call_log.append("fail-operation")
        return self._transition_operation(
            operation_id,
            OperationStatus.FAILED,
            authority,
        )

    def mark_needs_reconcile(
        self,
        operation_id: str,
        evidence: HostOperationEvidence,
        authority: RegistryAuthority,
    ) -> RegistryOperation:
        self.call_log.append("needs-reconcile")
        self.record_step(operation_id, evidence, authority)
        if (
            self._required_operation(operation_id).status
            == OperationStatus.NEEDS_RECONCILE.value
        ):
            return self._required_operation(operation_id)
        return self._transition_operation(
            operation_id,
            OperationStatus.NEEDS_RECONCILE,
            authority,
        )

    def cancel_operation(
        self,
        operation_id: str,
        authority: RegistryAuthority,
    ) -> RegistryOperation:
        self.call_log.append("cancel-operation")
        operation = self._required_operation(operation_id)
        allocation = next(
            (
                step
                for step in operation.steps
                if step.name == ProvisioningPhase.ALLOCATE.value
            ),
            None,
        )
        if allocation is not None and allocation.status == StepStatus.ROLLED_BACK.value:
            current = OperationStatus(operation.status)
            if current is not OperationStatus.ROLLING_BACK:
                validate_operation_transition(current, OperationStatus.ROLLING_BACK)
                operation = self._replace_operation(
                    operation,
                    status=OperationStatus.ROLLING_BACK.value,
                )
            validate_operation_transition(
                OperationStatus.ROLLING_BACK,
                OperationStatus.ROLLED_BACK,
            )
            return self._replace_operation(
                operation,
                status=OperationStatus.ROLLED_BACK.value,
            )
        return self._transition_operation(
            operation_id,
            OperationStatus.CANCELLED,
            authority,
        )

    def release_locks(self, operation_id: str) -> None:
        self.call_log.append("release-locks")
        self._scopes.pop(operation_id, None)

    def _transition_operation(
        self,
        operation_id: str,
        target: OperationStatus,
        authority: RegistryAuthority,
    ) -> RegistryOperation:
        self._check_authority(operation_id, authority)
        operation = self._required_operation(operation_id)
        current = OperationStatus(operation.status)
        if current is OperationStatus.RUNNING and target is OperationStatus.COMPLETED:
            operation = self._replace_operation(
                operation,
                status=OperationStatus.VERIFYING.value,
            )
            current = OperationStatus.VERIFYING
        validate_operation_transition(current, target)
        return self._replace_operation(operation, status=target.value)

    def _replace_operation(
        self,
        operation: RegistryOperation,
        *,
        status: str,
    ) -> RegistryOperation:
        updated = operation.model_copy(
            update={"status": status, "revision": operation.revision + 1}
        )
        self.operations[operation.id] = updated
        return updated

    def _check_authority(
        self,
        operation_id: str,
        authority: RegistryAuthority,
    ) -> None:
        if authority.operation_id != operation_id:
            raise RegistryConflictError("authority operation mismatch")
        current = self._tokens.get(authority.lock_scope)
        if current != authority.fencing_token:
            raise RegistryConflictError("stale fencing token")

    def _required_operation(self, operation_id: str) -> RegistryOperation:
        operation = self.operations.get(operation_id)
        if operation is None:
            raise RegistryConflictError(f"operation not found: {operation_id}")
        return operation

    def _required_resource(self, resource_id: str) -> RegistryResource:
        resource = self.resources.get(resource_id)
        if resource is None:
            raise RegistryConflictError(f"Resource not found: {resource_id}")
        return resource


def load_registry_client(plan: HostOperationPlan) -> HTTPRegistryClient:
    return HTTPRegistryClient.from_profile(plan.sources.registry_profile.path)


def _host_resource_plan(resource: RegistryResource) -> dict[str, object]:
    return {
        "resourceKey": resource.key,
        "sourceState": resource.lifecycle_state,
        "targetState": "ready",
        "resourceRevision": resource.revision,
        "lifecyclePath": [
            "absent",
            "allocated",
            "bootstrapped",
            "configured",
            "ready",
        ],
    }


def _host_binding_changes(
    plan: HostOperationPlan,
    resource_key: str,
) -> list[dict[str, object]]:
    return [
        {
            "action": "binding.replace",
            "resourceKey": resource_key,
            "providerId": plan.provider.adapter,
            "providerResourceType": plan.provider.resource_type,
        },
        {
            "action": "binding.remove",
            "resourceKey": resource_key,
        },
    ]


def resolve_secret_reference(reference: str | None) -> str:
    if reference is None:
        raise InputError("secret reference is missing")
    if reference.startswith("env:"):
        name = reference[4:]
        if not name or not name.replace("_", "A").isalnum() or name[0].isdigit():
            raise InputError("invalid environment secret reference")
        value = os.environ.get(name)
        if not value:
            raise InputError("referenced environment secret is not set")
        return value
    if reference.startswith("file:"):
        path = safe_file(reference[5:])
        value = path.read_text(encoding="utf-8").strip()
        if not value:
            raise InputError("referenced secret file is empty")
        return value
    raise InputError("secret values must use env: or file: references")


def provider_binding_matches(
    binding: dict[str, Any],
    evidence: ProviderEvidence,
) -> bool:
    """Return whether a Registry or in-memory Binding names this provider resource."""

    return (
        binding.get("providerId", binding.get("provider")) == evidence.provider
        and binding.get("providerResourceType", binding.get("resourceType"))
        == evidence.resource_type
        and binding.get("providerResourceId", binding.get("resourceId"))
        == evidence.resource_id
        and binding.get("providerResourceName", binding.get("resourceName"))
        == evidence.resource_name
        and binding.get("locator") == evidence.locator
    )


def _require_resource_authority(
    operation_id: str,
    resource_id: str,
    authority: RegistryAuthority,
) -> None:
    if (
        authority.operation_id != operation_id
        or authority.lock_scope != f"resource/{resource_id}"
    ):
        raise RegistryConflictError("authority does not cover the Resource")


def _resource_model(
    raw: dict[str, Any],
    binding: dict[str, Any] | None = None,
) -> RegistryResource:
    try:
        return RegistryResource(
            id=raw["id"],
            key=raw["key"],
            kind=raw["kind"],
            name=raw["name"],
            lifecycleState=raw["lifecycleState"],
            revision=raw["revision"],
            binding=binding,
        )
    except (KeyError, ValidationError) as exc:
        raise RegistryUnavailableError(
            "Global Registry returned an invalid Resource"
        ) from exc


def _operation_model(raw: dict[str, Any]) -> RegistryOperation:
    operation = raw.get("operation", raw)
    resources = raw.get("resources", [])
    raw_steps = raw.get("steps", [])
    if (
        not isinstance(operation, dict)
        or not isinstance(resources, list)
        or not isinstance(raw_steps, list)
        or not all(isinstance(resource, dict) for resource in resources)
        or not all(isinstance(step, dict) for step in raw_steps)
    ):
        raise RegistryUnavailableError("Global Registry returned an invalid operation")
    try:
        steps = [
            RegistryStep(
                id=step["id"],
                name=step["name"],
                status=step["status"],
                evidence=step.get("evidence", {}),
                revision=step["revision"],
            )
            for step in raw_steps
        ]
        return RegistryOperation(
            id=operation["id"],
            status=operation["status"],
            revision=operation["revision"],
            plan=operation.get("plan", {}),
            resources=resources,
            steps=steps,
        )
    except (KeyError, ValidationError) as exc:
        raise RegistryUnavailableError(
            "Global Registry returned an invalid operation"
        ) from exc


def _registry_step_status(status: StepStatus) -> str:
    return {
        StepStatus.PENDING: "planned",
        StepStatus.RUNNING: "running",
        StepStatus.SUCCEEDED: "succeeded",
        StepStatus.FAILED: "failed",
        StepStatus.SKIPPED: "skipped",
        StepStatus.NEEDS_RECONCILE: "blocked",
        StepStatus.ROLLED_BACK: "succeeded",
    }[status]


def _segment(value: str) -> str:
    return quote(value, safe="")
