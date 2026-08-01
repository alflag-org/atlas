from __future__ import annotations

import copy
import io
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from atlas_host_operations import (
    OperationStatus,
    ProvisioningPhase,
    ResourceLifecycle,
    StepStatus,
)
from atlas_host_operations.artifacts import (
    calculate_fingerprint,
    canonical_plan_payload,
    file_digest,
    git_state,
    load_host_spec,
    read_plan,
    read_yaml,
    reject_plaintext_secrets,
    safe_directory,
    safe_file,
    set_fingerprint,
    validate_fingerprint,
    validate_source_bindings,
    write_json,
)
from atlas_host_operations.errors import InputError, PlanError
from atlas_host_operations.lifecycle import (
    PROVISIONING_PHASES,
    phase_position,
    validate_operation_transition,
    validate_resource_transition,
    validate_step_transition,
)
from atlas_host_operations.models import (
    HostOperationEvidence,
    HostOperationPlan,
    HostSpec,
    SourceReference,
)
from pydantic import ValidationError

from .test_host_operations_support import make_host_fixture


def test_all_declared_lifecycle_transitions_and_terminal_states() -> None:
    valid_resources = {
        (ResourceLifecycle.ABSENT, ResourceLifecycle.PROVISIONING),
        (ResourceLifecycle.PROVISIONING, ResourceLifecycle.ACTIVE),
        (ResourceLifecycle.ACTIVE, ResourceLifecycle.MAINTENANCE),
        (ResourceLifecycle.ACTIVE, ResourceLifecycle.RETIRING),
        (ResourceLifecycle.MAINTENANCE, ResourceLifecycle.ACTIVE),
        (ResourceLifecycle.MAINTENANCE, ResourceLifecycle.RETIRING),
        (ResourceLifecycle.RETIRING, ResourceLifecycle.RETIRED),
    }
    for current in ResourceLifecycle:
        for target in ResourceLifecycle:
            if (current, target) in valid_resources:
                validate_resource_transition(current, target)
            else:
                with pytest.raises(PlanError, match="invalid resource transition"):
                    validate_resource_transition(current, target)

    valid_operations = {
        (OperationStatus.PLANNED, OperationStatus.LOCKED),
        (OperationStatus.PLANNED, OperationStatus.CANCELLED),
        (OperationStatus.LOCKED, OperationStatus.RUNNING),
        (OperationStatus.LOCKED, OperationStatus.CANCELLED),
        (OperationStatus.RUNNING, OperationStatus.VERIFYING),
        (OperationStatus.RUNNING, OperationStatus.FAILED),
        (OperationStatus.RUNNING, OperationStatus.NEEDS_RECONCILE),
        (OperationStatus.RUNNING, OperationStatus.ROLLING_BACK),
        (OperationStatus.VERIFYING, OperationStatus.COMPLETED),
        (OperationStatus.VERIFYING, OperationStatus.FAILED),
        (OperationStatus.VERIFYING, OperationStatus.NEEDS_RECONCILE),
        (OperationStatus.VERIFYING, OperationStatus.ROLLING_BACK),
        (OperationStatus.NEEDS_RECONCILE, OperationStatus.RUNNING),
        (OperationStatus.NEEDS_RECONCILE, OperationStatus.FAILED),
        (OperationStatus.NEEDS_RECONCILE, OperationStatus.ROLLING_BACK),
        (OperationStatus.ROLLING_BACK, OperationStatus.ROLLED_BACK),
        (OperationStatus.ROLLING_BACK, OperationStatus.FAILED),
        (OperationStatus.ROLLING_BACK, OperationStatus.NEEDS_RECONCILE),
    }
    for current in OperationStatus:
        for target in OperationStatus:
            if (current, target) in valid_operations:
                validate_operation_transition(current, target)
            else:
                with pytest.raises(PlanError, match="invalid operation transition"):
                    validate_operation_transition(current, target)

    valid_steps = {
        (StepStatus.PENDING, StepStatus.RUNNING),
        (StepStatus.PENDING, StepStatus.SKIPPED),
        (StepStatus.RUNNING, StepStatus.SUCCEEDED),
        (StepStatus.RUNNING, StepStatus.FAILED),
        (StepStatus.RUNNING, StepStatus.NEEDS_RECONCILE),
        (StepStatus.FAILED, StepStatus.RUNNING),
        (StepStatus.NEEDS_RECONCILE, StepStatus.RUNNING),
        (StepStatus.NEEDS_RECONCILE, StepStatus.FAILED),
        (StepStatus.SUCCEEDED, StepStatus.ROLLED_BACK),
    }
    for current in StepStatus:
        for target in StepStatus:
            if (current, target) in valid_steps:
                validate_step_transition(current, target)
            else:
                with pytest.raises(PlanError, match="invalid step transition"):
                    validate_step_transition(current, target)

    assert tuple(ProvisioningPhase) == PROVISIONING_PHASES
    assert phase_position(ProvisioningPhase.ACTIVATE) == 9


def test_strict_models_validate_unknown_fields_timezone_digest_and_paths(
    tmp_path: Path,
) -> None:
    fixture = make_host_fixture(tmp_path)
    plan = fixture.plan()
    assert plan.metadata.created_at.utcoffset() is not None
    assert plan.sources.host_spec.path == str(fixture.host_spec)
    assert plan.sources.host_spec.digest.startswith("sha256:")
    assert plan.as_artifact()["apiVersion"] == "atlas.host-operation/v1"

    data = plan.as_artifact()
    data["unexpected"] = True
    with pytest.raises(ValidationError, match="extra_forbidden"):
        HostOperationPlan.model_validate(data)
    data = plan.as_artifact()
    data["metadata"]["createdAt"] = "2026-08-01T00:00:00"
    with pytest.raises(ValidationError, match="timezone"):
        HostOperationPlan.model_validate(data)
    with pytest.raises(ValidationError, match="absolute"):
        SourceReference(path="relative.yml", digest="sha256:" + "0" * 64)
    with pytest.raises(ValidationError, match="sha256"):
        SourceReference(path="/tmp/input", digest="md5:bad")
    data = plan.as_artifact()
    data["metadata"]["fingerprint"] = "bad"
    with pytest.raises(ValidationError, match="fingerprint"):
        HostOperationPlan.model_validate(data)
    data = plan.as_artifact()
    data["resource"]["id"] = "Host_web01"
    with pytest.raises(ValidationError, match="string_pattern_mismatch"):
        HostOperationPlan.model_validate(data)
    data = plan.as_artifact()
    data["configuration"]["bootstrapPlaybook"] = "--help"
    with pytest.raises(ValidationError, match="string_pattern_mismatch"):
        HostOperationPlan.model_validate(data)
    data = plan.as_artifact()
    data["phases"] = data["phases"][:-1]
    data["metadata"].pop("fingerprint")
    with pytest.raises(ValidationError, match="complete provisioning order"):
        HostOperationPlan.model_validate(data)

    evidence = HostOperationEvidence(
        apiVersion="atlas.host-operation/v1",
        kind="HostOperationEvidence",
        operationId="op-1",
        planId=plan.metadata.plan_id,
        resourceId=plan.resource.id,
        phase="validate",
        status="succeeded",
        startedAt=datetime.now(UTC),
        finishedAt=datetime.now(UTC),
        attempt=1,
    )
    assert evidence.as_artifact()["phase"] == "validate"
    with pytest.raises(ValidationError, match="timezone"):
        evidence.model_copy(update={"started_at": datetime(2026, 1, 1)}).model_validate(
            {
                **evidence.as_artifact(),
                "startedAt": "2026-01-01T00:00:00",
            }
        )


def test_host_spec_rejects_unknown_fields_and_target_mismatch(tmp_path: Path) -> None:
    fixture = make_host_fixture(tmp_path)
    spec, path = load_host_spec(fixture.host_spec)
    assert isinstance(spec, HostSpec)
    assert path == fixture.host_spec
    data = read_yaml(fixture.host_spec)
    data["configuration"]["target"] = "other"
    fixture.host_spec.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(InputError, match="target must equal"):
        load_host_spec(fixture.host_spec)
    data["configuration"]["target"] = "web01"
    data["unknown"] = True
    fixture.host_spec.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(InputError, match="extra_forbidden"):
        load_host_spec(fixture.host_spec)

    data.pop("unknown")
    data["resource"]["id"] = "Host_web01"
    fixture.host_spec.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(InputError, match="string_pattern_mismatch"):
        load_host_spec(fixture.host_spec)

    data["resource"]["id"] = "h" * 129
    fixture.host_spec.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(InputError, match="string_too_long"):
        load_host_spec(fixture.host_spec)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("resource", "name", "-web01"),
        ("resource", "site", "../../outside"),
        ("resource", "zone", "/outside"),
        ("configuration", "target", "--help"),
        ("configuration", "bootstrap_playbook", "--help"),
        ("configuration", "converge_playbook", "../site"),
        ("readiness", "ssh_user", "-oProxyCommand=unsafe"),
    ],
)
def test_host_spec_rejects_unsafe_child_argument_names(
    tmp_path: Path,
    section: str,
    field: str,
    value: str,
) -> None:
    fixture = make_host_fixture(tmp_path)
    data = read_yaml(fixture.host_spec)
    data[section][field] = value
    fixture.host_spec.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(InputError, match="string_pattern_mismatch"):
        load_host_spec(fixture.host_spec)


def test_safe_source_helpers_reject_unsafe_or_invalid_sources(tmp_path: Path) -> None:
    source = tmp_path / "source.yml"
    source.write_text("value: 1\n", encoding="utf-8")
    assert safe_file(source) == source
    assert safe_file("source.yml", base=tmp_path) == source
    assert safe_directory(tmp_path) == tmp_path
    assert safe_directory(".", base=tmp_path) == tmp_path
    with pytest.raises(InputError, match="must be absolute"):
        safe_file("source.yml")
    with pytest.raises(InputError, match="must be absolute"):
        safe_directory("relative")
    with pytest.raises(InputError, match="not found or unsafe"):
        safe_file(tmp_path / "missing")
    with pytest.raises(InputError, match="not found or unsafe"):
        safe_directory(tmp_path / "missing-dir")
    link = tmp_path / "link.yml"
    link.symlink_to(source)
    with pytest.raises(InputError, match="unsafe"):
        safe_file(link)
    directory_link = tmp_path / "directory-link"
    directory_link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(InputError, match="unsafe"):
        safe_directory(directory_link)
    invalid = tmp_path / "invalid.yml"
    invalid.write_text("[", encoding="utf-8")
    with pytest.raises(InputError, match="not valid YAML"):
        read_yaml(invalid)
    invalid.write_text("- one\n", encoding="utf-8")
    with pytest.raises(InputError, match="mapping"):
        read_yaml(invalid)
    with pytest.raises(InputError, match="digest source"):
        file_digest(link)


def test_plaintext_secret_rejection_recurses_without_rejecting_refs() -> None:
    reject_plaintext_secrets(
        {
            "access_token": "env:ACCESS_TOKEN",
            "nested": [{"password": "file:/run/secrets/password"}],
            "ordinary": "value",
        }
    )
    with pytest.raises(InputError, match="plaintext secret"):
        reject_plaintext_secrets({"nested": [{"private_key": "plain"}]})


def test_plan_fingerprint_reading_and_source_binding(
    tmp_path: Path, monkeypatch
) -> None:
    fixture = make_host_fixture(tmp_path)
    plan = fixture.plan()
    validate_fingerprint(plan)
    assert calculate_fingerprint(plan) == plan.metadata.fingerprint
    canonical = canonical_plan_payload(plan)
    assert "fingerprint" not in canonical["metadata"]
    assert canonical_plan_payload(plan.as_artifact()) == canonical
    with pytest.raises(PlanError, match="metadata"):
        canonical_plan_payload({})
    unsigned = plan.model_copy(
        update={"metadata": plan.metadata.model_copy(update={"fingerprint": None})}
    )
    with pytest.raises(PlanError, match="missing"):
        validate_fingerprint(unsigned)
    changed = copy.deepcopy(plan.as_artifact())
    changed["resource"]["name"] = "other"
    changed["metadata"]["target"] = "other"
    changed["configuration"]["target"] = "other"
    changed_plan = HostOperationPlan.model_validate(changed)
    with pytest.raises(PlanError, match="invalid"):
        validate_fingerprint(changed_plan)
    assert (
        set_fingerprint(changed_plan).metadata.fingerprint != plan.metadata.fingerprint
    )

    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan.as_artifact()), encoding="utf-8")
    assert read_plan(str(plan_path)) == plan
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(plan.as_artifact())))
    assert read_plan("-") == plan
    monkeypatch.setattr("sys.stdin", io.StringIO("not-json"))
    with pytest.raises(PlanError, match="not valid JSON"):
        read_plan(None)
    plan_path.write_text("[]", encoding="utf-8")
    with pytest.raises(PlanError, match="JSON object"):
        read_plan(str(plan_path))
    plan_path.write_text("{}", encoding="utf-8")
    with pytest.raises(PlanError, match="plan is invalid"):
        read_plan(str(plan_path))

    validate_source_bindings(plan)
    fixture.provider_input.write_text("name: changed\n", encoding="utf-8")
    with pytest.raises(PlanError, match="provider input changed"):
        validate_source_bindings(plan)


def test_git_source_binding_detects_commit_and_dirty_changes(tmp_path: Path) -> None:
    fixture = make_host_fixture(tmp_path)
    plan = fixture.plan()
    commit, dirty = git_state(fixture.project)
    assert commit == plan.sources.provisioning_project.git_commit
    assert dirty is False
    (fixture.project / "new-file").write_text("dirty", encoding="utf-8")
    with pytest.raises(PlanError, match="dirty state"):
        validate_source_bindings(plan)
    with pytest.raises(InputError, match="Git checkout"):
        git_state(tmp_path)


def test_write_json_and_load_host_spec_error_paths(tmp_path: Path, capsys) -> None:
    write_json({"valid": True})
    assert json.loads(capsys.readouterr().out) == {"valid": True}
    with pytest.raises(TypeError, match="exceptions"):
        write_json(ValueError("bad"))
    invalid = tmp_path / "host.yml"
    invalid.write_text("- invalid\n", encoding="utf-8")
    with pytest.raises(InputError, match="mapping"):
        load_host_spec(invalid)
