from __future__ import annotations

import io
import json
from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from atlas_operations.operation.artifacts import (
    detect_artifact_kind,
    read_artifact_arg,
    write_diag_stderr,
    write_json_stdout,
    write_text_stdout,
)
from atlas_operations.operation.config import (
    ProviderDefinition,
    TemplateImage,
    is_secret_ref,
    load_provider_definition,
    load_vm_create_input,
    reject_plaintext_secrets,
    resolve_secret_ref,
)
from atlas_operations.operation.errors import (
    InputError,
    PlanError,
    ProviderError,
    SafetyError,
)
from atlas_operations.operation.evidence import OperationEvidence
from atlas_operations.operation.evidence_io import load_evidence
from atlas_operations.operation.files import file_digest
from atlas_operations.operation.fingerprint import (
    calculate_fingerprint,
    canonical_plan_payload,
    set_fingerprint,
    validate_fingerprint,
)
from atlas_operations.operation.io import input_file, read_json, read_yaml
from atlas_operations.operation.plan import CheckResult, OperationPlan
from atlas_operations.operation.plan_io import load_plan
from atlas_operations.operation.safety import SafetyGate
from atlas_operations.operation.validate import (
    _require_spec_keys,
    _validate_operation_spec,
    validate_artifact_data,
    validate_evidence,
    validate_plan,
    validate_plan_file,
)
from atlas_operations.operation.vm_create import (
    _created_resource as vm_created_resource,
)
from atlas_operations.operation.vm_create import (
    _evidence_plan as vm_evidence_plan,
)
from atlas_operations.operation.vm_create import (
    _network_cidr_check,
    _ping_check,
    _valid_hostname,
    apply_vm_create,
    build_vm_create_plan,
    rollback_vm_create,
    verify_vm_create,
)
from atlas_operations.operation.vm_template_create import (
    _created_resource as template_created_resource,
)
from atlas_operations.operation.vm_template_create import (
    _evidence_plan as template_evidence_plan,
)
from atlas_operations.operation.vm_template_create import (
    _local_image_preflight,
    _nearest_existing_parent,
    _prepare_shared_image,
    _runner_path_checks,
    _sha256_file,
    _verify_image_checksum,
    apply_vm_template_create,
    build_vm_template_create_plan,
    rollback_vm_template_create,
    verify_vm_template_create,
)
from pydantic import ValidationError

from .test_operation_support import FakeProvider, write_operation_inputs


def _plans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    provider: FakeProvider | None = None,
) -> tuple[ProviderDefinition, OperationPlan, OperationPlan, FakeProvider]:
    (
        provider_path,
        vm_input_path,
        template_input_path,
        definition,
        vm_input,
        template_input,
    ) = write_operation_inputs(tmp_path)
    monkeypatch.setattr(
        "atlas_operations.operation.vm_create._ping_check",
        lambda ip: CheckResult(name="network.ip.unused", status="passed", message=ip),
    )
    selected = provider or FakeProvider()
    vm_plan = build_vm_create_plan(
        definition,
        vm_input,
        input_path=str(vm_input_path),
        provider_path=str(provider_path),
        provider=selected,
    )
    template_plan = build_vm_template_create_plan(
        definition,
        template_input,
        input_path=str(template_input_path),
        provider_path=str(provider_path),
        provider=selected,
    )
    return definition, vm_plan, template_plan, selected


def _refingerprint(plan: OperationPlan, update) -> OperationPlan:
    data = plan.as_artifact()
    update(data)
    data["metadata"].pop("fingerprint", None)
    return set_fingerprint(OperationPlan.model_validate(data))


def _unsafe_refingerprint(plan: OperationPlan) -> OperationPlan:
    metadata = plan.metadata.model_copy(update={"fingerprint": None})
    without_fingerprint = plan.model_copy(update={"metadata": metadata})
    fingerprint = calculate_fingerprint(without_fingerprint)
    return without_fingerprint.model_copy(
        update={
            "metadata": without_fingerprint.metadata.model_copy(
                update={"fingerprint": fingerprint}
            )
        }
    )


def _write_artifact(path: Path, artifact: OperationPlan | OperationEvidence) -> None:
    path.write_text(
        json.dumps(artifact.as_artifact(), indent=2),
        encoding="utf-8",
    )


def test_explicit_inputs_generate_final_plans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition, vm_plan, template_plan, provider = _plans(tmp_path, monkeypatch)

    assert definition.provider == "proxmox"
    assert provider.capabilities().live_operations == [
        "proxmox.vm-create",
        "proxmox.vm-template-create",
    ]
    assert vm_plan.api_version == "atlas.operation/v1"
    assert vm_plan.metadata.operation_kind == "proxmox.vm-create"
    assert vm_plan.provider.mode == "live"
    assert vm_plan.preflight.status == "passed"
    assert vm_plan.spec["tags"][-2:] == ["managed-atlas", "platform-vm"]
    assert template_plan.metadata.operation_kind == "proxmox.vm-template-create"
    assert template_plan.spec["tags"][-2:] == [
        "managed-atlas",
        "platform-template",
    ]
    assert template_plan.preflight.status == "passed"
    for plan in (vm_plan, template_plan):
        assert Path(plan.source.input.path).is_absolute()
        assert Path(plan.source.provider.path).is_absolute()
        validate_plan(plan)
        validate_fingerprint(plan)
        assert calculate_fingerprint(plan).startswith("sha256:")
        assert "fingerprint" not in canonical_plan_payload(plan)["metadata"]


def test_vm_apply_verify_and_rollback_emit_explicit_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition, plan, _template_plan, provider = _plans(tmp_path, monkeypatch)
    progress: list[str] = []

    evidence = apply_vm_create(
        definition.safety,
        plan=plan,
        provider=provider,
        confirm=plan.metadata.plan_id,
        progress=progress.append,
    )

    assert evidence.metadata.result == "success"
    assert evidence.created_resources[0].ownership_marker_written is True
    assert evidence.rollback.supported is True
    assert progress[0] == "apply: clone-template"
    assert verify_vm_create(plan=plan, provider=provider).status == "passed"
    validate_evidence(evidence)

    rolled_back = rollback_vm_create(
        definition.safety,
        evidence=evidence,
        provider=provider,
        confirm=plan.metadata.plan_id,
        progress=progress.append,
    )
    assert rolled_back.rollback.result == "success"
    assert "delete-vm" in provider.rolled_back
    assert progress[-1] == "rollback: verify-deleted"
    validate_evidence(rolled_back)


@pytest.mark.parametrize(
    ("fail_on", "rollback_supported"),
    [
        ("clone-template", False),
        ("resize-disk", True),
    ],
)
def test_vm_partial_apply_records_rollback_boundary(
    fail_on: str,
    rollback_supported: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeProvider(fail_on=fail_on)
    definition, plan, _template_plan, _provider = _plans(
        tmp_path,
        monkeypatch,
        provider=provider,
    )
    evidence = apply_vm_create(
        definition.safety,
        plan=plan,
        provider=provider,
        confirm=plan.metadata.plan_id,
    )
    assert evidence.metadata.result == "failed"
    assert evidence.rollback.supported is rollback_supported


def test_vm_apply_verify_and_rollback_failures_are_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeProvider(verify_status="failed")
    definition, plan, _template_plan, _provider = _plans(
        tmp_path,
        monkeypatch,
        provider=provider,
    )
    evidence = apply_vm_create(
        definition.safety,
        plan=plan,
        provider=provider,
        confirm=plan.metadata.plan_id,
    )
    assert evidence.metadata.result == "failed"
    assert evidence.verify is not None
    assert evidence.verify.status == "failed"

    provider.rollback_fail_on = "delete-vm"
    rolled_back = rollback_vm_create(
        definition.safety,
        evidence=evidence,
        provider=provider,
        confirm=plan.metadata.plan_id,
    )
    assert rolled_back.metadata.result == "failed"
    assert rolled_back.rollback.result == "failed"


def test_template_apply_verify_and_rollback_emit_explicit_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition, _vm_plan, plan, provider = _plans(tmp_path, monkeypatch)
    progress: list[str] = []
    evidence = apply_vm_template_create(
        definition.safety,
        plan=plan,
        provider=provider,
        confirm=plan.metadata.plan_id,
        progress=progress.append,
    )
    assert evidence.metadata.result == "success"
    assert evidence.created_resources[0].type == "proxmox.qemu-template"
    assert evidence.created_resources[0].ownership_marker_written is True
    assert evidence.rollback.supported is True
    assert verify_vm_template_create(plan=plan, provider=provider).status == "passed"

    rolled_back = rollback_vm_template_create(
        definition.safety,
        evidence=evidence,
        provider=provider,
        confirm=plan.metadata.plan_id,
        progress=progress.append,
    )
    assert rolled_back.rollback.result == "success"
    assert "delete-template" in provider.rolled_back


@pytest.mark.parametrize(
    ("fail_on", "rollback_supported"),
    [
        ("download-image", False),
        ("import-disk", True),
    ],
)
def test_template_partial_apply_records_rollback_boundary(
    fail_on: str,
    rollback_supported: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeProvider(fail_on=fail_on)
    definition, _vm_plan, plan, _provider = _plans(
        tmp_path,
        monkeypatch,
        provider=provider,
    )
    if fail_on == "download-image":
        monkeypatch.setattr(
            "atlas_operations.operation.vm_template_create._prepare_shared_image",
            lambda plan: (_ for _ in ()).throw(ProviderError("download failed")),
        )
    evidence = apply_vm_template_create(
        definition.safety,
        plan=plan,
        provider=provider,
        confirm=plan.metadata.plan_id,
    )
    assert evidence.metadata.result == "failed"
    assert evidence.rollback.supported is rollback_supported


def test_template_verify_and_rollback_failure_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeProvider(verify_status="failed")
    definition, _vm_plan, plan, _provider = _plans(
        tmp_path,
        monkeypatch,
        provider=provider,
    )
    evidence = apply_vm_template_create(
        definition.safety,
        plan=plan,
        provider=provider,
        confirm=plan.metadata.plan_id,
    )
    assert evidence.metadata.result == "failed"
    provider.rollback_fail_on = "delete-template"
    rolled_back = rollback_vm_template_create(
        definition.safety,
        evidence=evidence,
        provider=provider,
        confirm=plan.metadata.plan_id,
    )
    assert rolled_back.rollback.result == "failed"


def test_template_apply_refuses_missing_image_preparation_steps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition, _vm_plan, plan, provider = _plans(tmp_path, monkeypatch)

    without_download = _refingerprint(
        plan,
        lambda data: data["apply"]["steps"].pop(0),
    )
    evidence = apply_vm_template_create(
        definition.safety,
        plan=without_download,
        provider=provider,
        confirm=without_download.metadata.plan_id,
    )
    assert evidence.metadata.result == "failed"
    assert "image has not been prepared" in evidence.steps[0].message

    without_local_steps = _refingerprint(
        plan,
        lambda data: data["apply"]["steps"].__setitem__(
            slice(0, 2),
            [],
        ),
    )
    evidence = apply_vm_template_create(
        definition.safety,
        plan=without_local_steps,
        provider=provider,
        confirm=without_local_steps.metadata.plan_id,
    )
    assert evidence.metadata.result == "failed"
    assert evidence.steps[-1].id == "import-disk"
    assert "image has not been prepared" in evidence.steps[-1].message


def test_safety_gate_rejects_apply_without_exact_fresh_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition, plan, _template_plan, _provider = _plans(tmp_path, monkeypatch)
    gate = SafetyGate(definition.safety, now=plan.metadata.created_at)
    with pytest.raises(SafetyError, match="--confirm"):
        gate.validate_apply(plan, confirm=None, preflight_passed=True)
    with pytest.raises(SafetyError, match="preflight"):
        gate.validate_apply(
            plan,
            confirm=plan.metadata.plan_id,
            preflight_passed=False,
        )

    future = _refingerprint(
        plan,
        lambda data: data["metadata"].update(
            createdAt=(plan.metadata.created_at + timedelta(seconds=1)).isoformat()
        ),
    )
    with pytest.raises(SafetyError, match="future"):
        gate.validate_apply(
            future,
            confirm=future.metadata.plan_id,
            preflight_passed=True,
        )

    expired = _refingerprint(
        plan,
        lambda data: data["metadata"].update(
            createdAt=(
                plan.metadata.created_at
                - timedelta(seconds=definition.safety.max_plan_age_seconds + 1)
            ).isoformat()
        ),
    )
    with pytest.raises(SafetyError, match="max_plan_age_seconds"):
        gate.validate_apply(
            expired,
            confirm=expired.metadata.plan_id,
            preflight_passed=True,
        )


def test_safety_gate_rejects_unbound_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition, plan, _template_plan, provider = _plans(tmp_path, monkeypatch)
    evidence = apply_vm_create(
        definition.safety,
        plan=plan,
        provider=provider,
        confirm=plan.metadata.plan_id,
    )

    with pytest.raises(SafetyError, match="--confirm"):
        SafetyGate(definition.safety).validate_rollback(
            plan,
            evidence,
            confirm="wrong",
        )
    disabled = definition.safety.model_copy(update={"allow_rollback_delete": False})
    with pytest.raises(SafetyError, match="disabled"):
        SafetyGate(disabled).validate_rollback(
            plan,
            evidence,
            confirm=plan.metadata.plan_id,
        )
    policy_disabled = _refingerprint(
        plan,
        lambda data: data["spec"]["policy"].update(rollbackDeleteAllowed=False),
    )
    with pytest.raises(SafetyError, match="operation input"):
        SafetyGate(definition.safety).validate_rollback(
            policy_disabled,
            evidence,
            confirm=policy_disabled.metadata.plan_id,
        )


def test_artifact_roundtrip_and_strict_parsing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _definition, plan, _template_plan, _provider = _plans(tmp_path, monkeypatch)
    plan_path = tmp_path / "plan.json"
    _write_artifact(plan_path, plan)

    assert input_file(plan_path) == plan_path
    assert read_json(plan_path)["kind"] == "OperationPlan"
    assert read_artifact_arg(str(plan_path))["kind"] == "OperationPlan"
    assert detect_artifact_kind(read_artifact_arg(str(plan_path))) == "OperationPlan"
    assert load_plan(plan_path).metadata.plan_id == plan.metadata.plan_id
    assert validate_plan_file(plan_path).metadata.plan_id == plan.metadata.plan_id
    assert validate_artifact_data(read_json(plan_path)).kind == "OperationPlan"

    write_json_stdout({"valid": True})
    write_text_stdout("plain")
    write_diag_stderr("diagnostic")
    streams = capsys.readouterr()
    assert '"valid": true' in streams.out
    assert "plain" in streams.out
    assert "diagnostic" in streams.err

    with pytest.raises(PlanError, match="kind is missing"):
        detect_artifact_kind({})
    with pytest.raises(PlanError, match="unsafe"):
        read_artifact_arg(str(tmp_path / "missing.json"))

    bad = tmp_path / "bad.json"
    bad.write_text("[1]", encoding="utf-8")
    with pytest.raises(PlanError, match="JSON object"):
        read_json(bad)
    bad.write_text("{", encoding="utf-8")
    with pytest.raises(PlanError, match="not valid JSON"):
        read_json(bad)
    with pytest.raises(PlanError, match="not valid JSON"):
        read_artifact_arg(str(bad))


def test_artifact_readers_reject_internal_field_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition, plan, _template_plan, provider = _plans(tmp_path, monkeypatch)

    internal_plan = plan.as_artifact()
    internal_plan["api_version"] = internal_plan.pop("apiVersion")
    with pytest.raises(PlanError, match="invalid"):
        validate_artifact_data(internal_plan)
    plan_path = tmp_path / "internal-plan.json"
    plan_path.write_text(json.dumps(internal_plan), encoding="utf-8")
    with pytest.raises(PlanError, match="invalid"):
        load_plan(plan_path)

    nested_internal_plan = plan.as_artifact()
    nested_internal_plan["metadata"]["plan_id"] = nested_internal_plan["metadata"].pop(
        "planId"
    )
    with pytest.raises(PlanError, match="invalid"):
        validate_artifact_data(nested_internal_plan)

    evidence = apply_vm_create(
        definition.safety,
        plan=plan,
        provider=provider,
        confirm=plan.metadata.plan_id,
    )
    internal_evidence = evidence.as_artifact()
    internal_evidence["created_resources"] = internal_evidence.pop("createdResources")
    with pytest.raises(PlanError, match="invalid"):
        validate_artifact_data(internal_evidence)
    evidence_path = tmp_path / "internal-evidence.json"
    evidence_path.write_text(json.dumps(internal_evidence), encoding="utf-8")
    with pytest.raises(PlanError, match="invalid"):
        load_evidence(evidence_path)


def test_yaml_and_digest_helpers_reject_unsafe_inputs(tmp_path: Path) -> None:
    mapping = tmp_path / "mapping.yml"
    mapping.write_text("key: value\n", encoding="utf-8")
    assert read_yaml(mapping) == {"key": "value"}
    assert file_digest(mapping).startswith("sha256:")

    sequence = tmp_path / "sequence.yml"
    sequence.write_text("- value\n", encoding="utf-8")
    with pytest.raises(InputError, match="YAML mapping"):
        read_yaml(sequence)
    invalid = tmp_path / "invalid.yml"
    invalid.write_text("key: [\n", encoding="utf-8")
    with pytest.raises(InputError, match="not valid YAML"):
        read_yaml(invalid)

    link = tmp_path / "link.yml"
    link.symlink_to(mapping)
    with pytest.raises(InputError, match="unsafe"):
        input_file(link)
    with pytest.raises(InputError, match="unsafe"):
        file_digest(link)


def test_config_secret_references_are_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_path, vm_path, _template_path, definition, _vm, _template = (
        write_operation_inputs(tmp_path)
    )
    assert load_provider_definition(provider_path) == definition
    assert load_vm_create_input(vm_path).kind == "ProxmoxVmCreate"
    assert is_secret_ref("env:TOKEN")
    assert is_secret_ref("file:/run/secrets/token")
    assert not is_secret_ref("plain")

    monkeypatch.setenv("PROXMOX_TOKEN", "secret-value")
    assert resolve_secret_ref("env:PROXMOX_TOKEN") == "secret-value"
    secret = tmp_path / "secret"
    secret.write_text("file-secret\n", encoding="utf-8")
    assert resolve_secret_ref(f"file:{secret}") == "file-secret"

    with pytest.raises(InputError, match="plaintext"):
        reject_plaintext_secrets({"nested": {"token": "secret"}})
    reject_plaintext_secrets({"nested": [{"token": "env:TOKEN"}]})
    with pytest.raises(InputError, match="unsupported"):
        resolve_secret_ref("literal")
    with pytest.raises(InputError, match="not set"):
        resolve_secret_ref("env:MISSING_OPERATION_TOKEN")
    with pytest.raises(InputError, match="invalid environment"):
        resolve_secret_ref("env:bad-name")
    with pytest.raises(InputError, match="absolute"):
        resolve_secret_ref("file:relative")
    empty = tmp_path / "empty-secret"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(InputError, match="empty"):
        resolve_secret_ref(f"file:{empty}")


def test_config_models_reject_legacy_or_ambiguous_values(tmp_path: Path) -> None:
    provider_path, vm_path, _template_path, definition, vm_input, template_input = (
        write_operation_inputs(tmp_path)
    )
    raw_provider = yaml.safe_load(provider_path.read_text(encoding="utf-8"))
    raw_provider["connection"]["token_secret"] = "plain"
    provider_path.write_text(yaml.safe_dump(raw_provider), encoding="utf-8")
    with pytest.raises(InputError, match="plaintext"):
        load_provider_definition(provider_path)

    internal_name_provider = definition.model_dump(mode="python", by_alias=True)
    internal_name_provider["schema_version"] = internal_name_provider.pop("schema")
    provider_path.write_text(
        yaml.safe_dump(internal_name_provider),
        encoding="utf-8",
    )
    with pytest.raises(InputError, match="invalid"):
        load_provider_definition(provider_path)

    raw_vm = yaml.safe_load(vm_path.read_text(encoding="utf-8"))
    raw_vm["ares"] = {}
    vm_path.write_text(yaml.safe_dump(raw_vm), encoding="utf-8")
    with pytest.raises(InputError, match="invalid"):
        load_vm_create_input(vm_path)

    with pytest.raises(ValidationError, match="https"):
        TemplateImage(
            url="http://example.invalid/image",
            checksum="sha256:" + "0" * 64,
            shared_path=tmp_path / "image",
        )
    with pytest.raises(ValidationError, match="cannot be combined"):
        TemplateImage(
            url="https://example.invalid/image",
            checksum="sha256:" + "0" * 64,
            shared_path=tmp_path / "image",
            runner_path=tmp_path / "runner",
        )
    with pytest.raises(ValidationError, match="both runner_path and node_path"):
        TemplateImage(
            url="https://example.invalid/image",
            checksum="sha256:" + "0" * 64,
            runner_path=tmp_path / "runner",
        )

    provider_data = definition.model_dump(mode="python", by_alias=True)
    provider_data["connection"]["api_url"] = "ftp://pve.example.invalid"
    with pytest.raises(ValidationError, match="http"):
        ProviderDefinition.model_validate(provider_data)
    provider_data = definition.model_dump(mode="python", by_alias=True)
    provider_data["connection"]["api_url"] = "http://pve.example.invalid"
    with pytest.raises(ValidationError, match="https"):
        ProviderDefinition.model_validate(provider_data)
    provider_data = definition.model_dump(mode="python", by_alias=True)
    provider_data["connection"]["api_url"] = "https://user@pve.example.invalid?x=1"
    with pytest.raises(ValidationError, match="credentials"):
        ProviderDefinition.model_validate(provider_data)
    provider_data["connection"]["api_url"] = "https://pve.example.invalid"
    provider_data["connection"]["token_secret_ref"] = "plain"
    with pytest.raises(ValidationError, match="env: or file:"):
        ProviderDefinition.model_validate(provider_data)

    vm_data = vm_input.model_dump(mode="python", by_alias=True)
    vm_data["vm"]["name"] = "Bad_Name"
    with pytest.raises(ValidationError, match="DNS label"):
        type(vm_input).model_validate(vm_data)
    vm_data = vm_input.model_dump(mode="python", by_alias=True)
    vm_data["target"] = "other"
    with pytest.raises(ValidationError, match="target must equal"):
        type(vm_input).model_validate(vm_data)

    with pytest.raises(ValidationError, match="checksum"):
        TemplateImage(
            url="https://example.invalid/image",
            checksum="sha256:bad",
            shared_path=tmp_path / "image",
        )
    with pytest.raises(ValidationError, match="absolute"):
        TemplateImage(
            url="https://example.invalid/image",
            checksum="sha256:" + "0" * 64,
            shared_path=Path("relative"),
        )

    template_data = template_input.model_dump(mode="python", by_alias=True)
    template_data["target"] = "other"
    with pytest.raises(ValidationError, match="target must equal"):
        type(template_input).model_validate(template_data)
    template_data = template_input.model_dump(mode="python", by_alias=True)
    template_data["name"] = "Bad_Name"
    template_data["target"] = "Bad_Name"
    with pytest.raises(ValidationError, match="DNS label"):
        type(template_input).model_validate(template_data)

    with pytest.raises(InputError, match="missing or unsafe"):
        resolve_secret_ref(f"file:{tmp_path / 'missing-secret'}")

    vm_data = vm_input.model_dump(mode="python", by_alias=True)
    vm_data["tags"] = ["Bad_Tag"]
    with pytest.raises(ValidationError, match="tags must"):
        type(vm_input).model_validate(vm_data)
    vm_data = vm_input.model_dump(mode="python", by_alias=True)
    vm_data["guest"]["qemu_agent"] = False
    with pytest.raises(ValidationError, match="True"):
        type(vm_input).model_validate(vm_data)
    template_data = template_input.model_dump(mode="python", by_alias=True)
    template_data["tags"] = ["Bad_Tag"]
    with pytest.raises(ValidationError, match="tags must"):
        type(template_input).model_validate(template_data)
    template_data = template_input.model_dump(mode="python", by_alias=True)
    template_data["guest"]["serial_console"] = False
    with pytest.raises(ValidationError, match="True"):
        type(template_input).model_validate(template_data)
    provider_data = definition.model_dump(mode="python", by_alias=True)
    provider_data["safety"]["require_confirm"] = False
    with pytest.raises(ValidationError, match="True"):
        ProviderDefinition.model_validate(provider_data)

    with pytest.raises(ValidationError, match="credentials"):
        TemplateImage(
            url="https://user@example.invalid/image?token=value",
            checksum="sha256:" + "0" * 64,
            shared_path=tmp_path / "image",
        )
    normalized = TemplateImage(
        url="https://example.invalid/image",
        checksum="sha256:" + "A" * 64,
        shared_path=tmp_path / "image",
    )
    assert normalized.checksum == "sha256:" + "a" * 64


def test_plan_and_evidence_validation_detect_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition, plan, _template_plan, provider = _plans(tmp_path, monkeypatch)
    data = plan.as_artifact()
    data["spec"]["memoryMb"] = 4096
    tampered = OperationPlan.model_validate(data)
    with pytest.raises(PlanError, match="fingerprint"):
        validate_fingerprint(tampered)

    data = plan.as_artifact()
    data["apiVersion"] = "old"
    with pytest.raises(PlanError, match="invalid"):
        validate_artifact_data(data)
    with pytest.raises(PlanError, match="unsupported artifact kind"):
        validate_artifact_data({"kind": "Unknown"})
    with pytest.raises(PlanError, match="unsupported artifact kind"):
        validate_artifact_data({})

    evidence = apply_vm_create(
        definition.safety,
        plan=plan,
        provider=provider,
        confirm=plan.metadata.plan_id,
    )
    evidence_path = tmp_path / "evidence.json"
    _write_artifact(evidence_path, evidence)
    assert load_evidence(evidence_path).metadata.plan_id == plan.metadata.plan_id
    assert validate_artifact_data(read_json(evidence_path)).kind == "OperationEvidence"

    evidence_data = evidence.as_artifact()
    evidence_data["plan"]["fingerprint"] = "sha256:" + "0" * 64
    bad_evidence = OperationEvidence.model_validate(evidence_data)
    with pytest.raises(PlanError, match="fingerprint"):
        validate_evidence(bad_evidence)

    invalid_fingerprint = plan.as_artifact()
    invalid_fingerprint["metadata"]["fingerprint"] = "sha256:bad"
    with pytest.raises(ValidationError, match="fingerprint must"):
        OperationPlan.model_validate(invalid_fingerprint)
    relative_source = plan.as_artifact()
    relative_source["source"]["input"]["path"] = "relative.yml"
    with pytest.raises(ValidationError, match="source path"):
        OperationPlan.model_validate(relative_source)
    invalid_digest = plan.as_artifact()
    invalid_digest["source"]["input"]["digest"] = "sha256:bad"
    with pytest.raises(ValidationError, match="source digest"):
        OperationPlan.model_validate(invalid_digest)
    invalid_evidence_fingerprint = evidence.as_artifact()
    invalid_evidence_fingerprint["plan"]["fingerprint"] = "sha256:bad"
    with pytest.raises(ValidationError, match="fingerprint must"):
        OperationEvidence.model_validate(invalid_evidence_fingerprint)


def test_operation_helper_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _valid_hostname("web01")
    assert not _valid_hostname("")
    assert not _valid_hostname("-web")
    assert not _valid_hostname("web_01")
    assert not _valid_hostname("a" * 64)
    assert _network_cidr_check("192.0.2.21", 24, "192.0.2.1").status == "passed"
    assert _network_cidr_check("not-an-ip", 24, "192.0.2.1").status == "failed"

    monkeypatch.setattr(
        "atlas_operations.operation.vm_create.subprocess.run",
        lambda *args, **kwargs: type("Process", (), {"returncode": 1})(),
    )
    assert _ping_check("192.0.2.21").status == "passed"
    monkeypatch.setattr(
        "atlas_operations.operation.vm_create.subprocess.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    )
    assert _ping_check("192.0.2.21").status == "warning"

    assert _nearest_existing_parent(tmp_path / "nested/image") == tmp_path
    assert _runner_path_checks(tmp_path / "nested/image")[0].status == "passed"
    link = tmp_path / "image-link"
    target = tmp_path / "target"
    target.write_bytes(b"data")
    link.symlink_to(target)
    assert _runner_path_checks(link)[0].status == "failed"

    monkeypatch.setattr(Path, "exists", lambda self: False)
    assert _nearest_existing_parent(tmp_path / "nowhere/image") is None


def test_template_image_prepare_downloads_atomically_and_preserves_bad_existing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        provider_path,
        _vm_input_path,
        template_input_path,
        definition,
        _vm_input,
        template_input,
    ) = write_operation_inputs(tmp_path)
    destination = tmp_path / "downloaded/image.img"
    payload = b"downloaded fixture"
    checksum = "sha256:" + __import__("hashlib").sha256(payload).hexdigest()
    raw = template_input.model_dump(mode="python", by_alias=True)
    raw["image"] = {
        "url": "https://images.example.invalid/download.img",
        "checksum": checksum,
        "runner_path": destination,
        "node_path": Path("/srv/images/download.img"),
    }
    template_input = type(template_input).model_validate(raw)
    plan = build_vm_template_create_plan(
        definition,
        template_input,
        input_path=str(template_input_path),
        provider_path=str(provider_path),
        provider=FakeProvider(),
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size: int = -1) -> bytes:
            if getattr(self, "read_once", False):
                return b""
            self.read_once = True
            return payload

        def geturl(self) -> str:
            return "https://images.example.invalid/download.img"

    monkeypatch.setattr(
        "atlas_operations.operation.vm_template_create.urllib.request.urlopen",
        lambda url, timeout: Response(),
    )
    assert _prepare_shared_image(plan) == (destination, "/srv/images/download.img")
    assert destination.read_bytes() == payload
    assert _sha256_file(destination) == checksum

    destination.write_bytes(b"wrong")
    with pytest.raises(ProviderError, match="checksum mismatch"):
        _verify_image_checksum(destination, checksum)
    assert destination.read_bytes() == b"wrong"


def test_template_image_prepare_rejects_unsafe_paths_and_cleans_partial_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _definition, _vm_plan, plan, _provider = _plans(tmp_path, monkeypatch)

    missing_transfer = plan.model_copy(
        update={
            "spec": {
                **plan.spec,
                "image": {
                    **plan.spec["image"],
                    "transfer": {"mode": "copy"},
                },
            }
        }
    )
    assert _local_image_preflight(missing_transfer).status == "failed"
    with pytest.raises(ProviderError, match="requires transfer"):
        _prepare_shared_image(missing_transfer)

    target = tmp_path / "unsafe-target"
    target.write_bytes(b"data")
    link = tmp_path / "unsafe-link"
    link.symlink_to(target)
    symlink_plan = plan.model_copy(
        update={
            "spec": {
                **plan.spec,
                "image": {
                    **plan.spec["image"],
                    "transfer": {
                        **plan.spec["image"]["transfer"],
                        "runnerPath": str(link),
                    },
                },
            }
        }
    )
    with pytest.raises(ProviderError, match="unsafe"):
        _prepare_shared_image(symlink_plan)

    directory = tmp_path / "image-directory"
    directory.mkdir()
    directory_plan = plan.model_copy(
        update={
            "spec": {
                **plan.spec,
                "image": {
                    **plan.spec["image"],
                    "transfer": {
                        **plan.spec["image"]["transfer"],
                        "runnerPath": str(directory),
                    },
                },
            }
        }
    )
    with pytest.raises(ProviderError, match="unsafe"):
        _prepare_shared_image(directory_plan)

    redirect_destination = tmp_path / "redirect/image.img"
    redirect_plan = plan.model_copy(
        update={
            "spec": {
                **plan.spec,
                "image": {
                    **plan.spec["image"],
                    "transfer": {
                        **plan.spec["image"]["transfer"],
                        "runnerPath": str(redirect_destination),
                    },
                },
            }
        }
    )

    class UnsafeRedirect:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def geturl(self) -> str:
            return "http://images.example.invalid/image.img"

    monkeypatch.setattr(
        "atlas_operations.operation.vm_template_create.urllib.request.urlopen",
        lambda url, timeout: UnsafeRedirect(),
    )
    with pytest.raises(ProviderError, match="redirect is unsafe"):
        _prepare_shared_image(redirect_plan)
    assert list(redirect_destination.parent.glob(".*.tmp")) == []

    destination = tmp_path / "failed-download/image.img"
    download_plan = plan.model_copy(
        update={
            "spec": {
                **plan.spec,
                "image": {
                    **plan.spec["image"],
                    "transfer": {
                        **plan.spec["image"]["transfer"],
                        "runnerPath": str(destination),
                    },
                },
            }
        }
    )
    monkeypatch.setattr(
        "atlas_operations.operation.vm_template_create.urllib.request.urlopen",
        lambda url, timeout: (_ for _ in ()).throw(OSError("download failed")),
    )
    with pytest.raises(OSError, match="download failed"):
        _prepare_shared_image(download_plan)
    assert list(destination.parent.glob(".*.tmp")) == []


def test_created_resource_lookup_requires_matching_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition, plan, template_plan, provider = _plans(tmp_path, monkeypatch)
    evidence = apply_vm_create(
        definition.safety,
        plan=plan,
        provider=provider,
        confirm=plan.metadata.plan_id,
    )
    assert vm_created_resource(evidence, 121).id == "qemu/121"
    wrong_vm_resource = evidence.created_resources[0].model_copy(
        update={"vmid": 999}
    )
    assert vm_created_resource(
        evidence.model_copy(
            update={
                "created_resources": [
                    wrong_vm_resource,
                    *evidence.created_resources,
                ]
            }
        ),
        121,
    ).id == "qemu/121"
    with pytest.raises(ProviderError, match="created resource"):
        vm_created_resource(evidence.model_copy(update={"created_resources": []}), 121)

    template_evidence = apply_vm_template_create(
        definition.safety,
        plan=template_plan,
        provider=provider,
        confirm=template_plan.metadata.plan_id,
    )
    assert template_created_resource(template_evidence, 9100).id == "qemu/9100"
    wrong_template_resource = template_evidence.created_resources[0].model_copy(
        update={"type": "other"}
    )
    assert template_created_resource(
        template_evidence.model_copy(
            update={
                "created_resources": [
                    wrong_template_resource,
                    *template_evidence.created_resources,
                ]
            }
        ),
        9100,
    ).id == "qemu/9100"
    with pytest.raises(ProviderError, match="created resource"):
        template_created_resource(
            template_evidence.model_copy(update={"created_resources": []}),
            9100,
        )


def test_safety_gate_checks_every_rollback_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition, plan, _template_plan, provider = _plans(tmp_path, monkeypatch)
    evidence = apply_vm_create(
        definition.safety,
        plan=plan,
        provider=provider,
        confirm=plan.metadata.plan_id,
    )
    gate = SafetyGate(definition.safety)
    unsupported_plan = _unsafe_refingerprint(
        plan.model_copy(
            update={
                "rollback": plan.rollback.model_copy(update={"supported": False})
            }
        )
    )
    unsupported_evidence = evidence.model_copy(
        update={
            "plan": evidence.plan.model_copy(
                update={
                    "fingerprint": unsupported_plan.metadata.fingerprint,
                    "snapshot": unsupported_plan.as_artifact(),
                }
            )
        }
    )

    cases = [
        (
            evidence.model_copy(
                update={"plan": evidence.plan.model_copy(update={"snapshot": {}})}
            ),
            plan,
            "plan snapshot",
        ),
        (
            evidence.model_copy(
                update={
                    "plan": evidence.plan.model_copy(
                        update={"fingerprint": "sha256:wrong"}
                    )
                }
            ),
            plan,
            "fingerprint",
        ),
        (
            unsupported_evidence,
            unsupported_plan,
            "does not support",
        ),
        (
            evidence.model_copy(
                update={
                    "metadata": evidence.metadata.model_copy(
                        update={"plan_id": "other"}
                    )
                }
            ),
            plan,
            "different plan",
        ),
        (
            evidence.model_copy(
                update={
                    "metadata": evidence.metadata.model_copy(
                        update={"operation_kind": "other"}
                    )
                }
            ),
            plan,
            "operation kind",
        ),
        (
            evidence.model_copy(
                update={
                    "metadata": evidence.metadata.model_copy(update={"target": "other"})
                }
            ),
            plan,
            "target",
        ),
        (
            evidence.model_copy(
                update={
                    "provider": evidence.provider.model_copy(update={"name": "other"})
                }
            ),
            plan,
            "provider does not match",
        ),
        (
            evidence.model_copy(
                update={
                    "provider": evidence.provider.model_copy(update={"node": "other"})
                }
            ),
            plan,
            "provider node",
        ),
        (
            evidence.model_copy(
                update={"provider": evidence.provider.model_copy(update={"vmid": 999})}
            ),
            plan,
            "VMID",
        ),
        (
            evidence.model_copy(
                update={
                    "rollback": evidence.rollback.model_copy(
                        update={"supported": False}
                    )
                }
            ),
            plan,
            "does not allow",
        ),
        (
            evidence.model_copy(update={"created_resources": []}),
            plan,
            "does not show",
        ),
    ]
    for candidate_evidence, candidate_plan, message in cases:
        with pytest.raises(SafetyError, match=message):
            gate.validate_rollback(
                candidate_plan,
                candidate_evidence,
                confirm=candidate_plan.metadata.plan_id,
            )

    unknown = plan.model_copy(
        update={
            "metadata": plan.metadata.model_copy(update={"operation_kind": "unknown"})
        }
    )
    with pytest.raises(SafetyError, match="not implemented"):
        gate.validate_apply(
            unknown,
            confirm=unknown.metadata.plan_id,
            preflight_passed=True,
        )

    disabled_mode = _unsafe_refingerprint(
        plan.model_copy(
            update={"provider": plan.provider.model_copy(update={"mode": "disabled"})}
        )
    )
    with pytest.raises(SafetyError, match="mode must be live"):
        gate.validate_apply(
            disabled_mode,
            confirm=disabled_mode.metadata.plan_id,
            preflight_passed=True,
        )


def test_plan_validation_rejects_each_invalid_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _definition, plan, template_plan, _provider = _plans(tmp_path, monkeypatch)

    unknown = plan.model_copy(
        update={
            "metadata": plan.metadata.model_copy(update={"operation_kind": "unknown"})
        }
    )
    with pytest.raises(PlanError, match="not implemented"):
        validate_plan(unknown)

    wrong_provider = plan.model_copy(
        update={"provider": plan.provider.model_copy(update={"name": "other"})}
    )
    with pytest.raises(PlanError, match="provider mismatch"):
        validate_plan(wrong_provider)

    no_rollback = _unsafe_refingerprint(
        plan.model_copy(
            update={"rollback": plan.rollback.model_copy(update={"supported": False})}
        )
    )
    with pytest.raises(PlanError, match="requiresRollback"):
        validate_plan(no_rollback)

    step_cases = [
        ("id", "", "step id"),
        ("action", "", "step action"),
        ("provider", "other", "provider mismatch"),
    ]
    for field, value, message in step_cases:
        first = plan.apply.steps[0].model_copy(update={field: value})
        candidate = _unsafe_refingerprint(
            plan.model_copy(
                update={
                    "apply": plan.apply.model_copy(
                        update={"steps": [first, *plan.apply.steps[1:]]}
                    )
                }
            )
        )
        with pytest.raises(PlanError, match=message):
            validate_plan(candidate)

    changed_action = _unsafe_refingerprint(
        plan.model_copy(
            update={
                "apply": plan.apply.model_copy(
                    update={
                        "steps": [
                            plan.apply.steps[0].model_copy(
                                update={"action": "set-tags"}
                            ),
                            *plan.apply.steps[1:],
                        ]
                    }
                )
            }
        )
    )
    with pytest.raises(PlanError, match=r"implemented .* contract"):
        validate_plan(changed_action)

    for key in ("policy", "disk"):
        candidate = _refingerprint(plan, lambda data, key=key: data["spec"].pop(key))
        with pytest.raises(PlanError, match="missing required"):
            validate_plan(candidate)
    candidate = _refingerprint(
        plan,
        lambda data: data["spec"]["disk"].pop("device"),
    )
    with pytest.raises(PlanError, match="missing required"):
        validate_plan(candidate)
    candidate = _refingerprint(
        plan,
        lambda data: data["spec"]["network"].pop("gateway"),
    )
    with pytest.raises(PlanError, match="missing required"):
        validate_plan(candidate)

    transfer_cases = [
        ("mode", "copy", "mode"),
        ("runnerPath", "", "runnerPath"),
        ("nodePath", "", "nodePath"),
    ]
    for key, value, message in transfer_cases:
        candidate = _refingerprint(
            template_plan,
            lambda data, key=key, value=value: data["spec"]["image"][
                "transfer"
            ].update({key: value}),
        )
        with pytest.raises(PlanError, match=message):
            validate_plan(candidate)
    candidate = _refingerprint(
        template_plan,
        lambda data: data["spec"]["resources"].pop("diskDevice"),
    )
    with pytest.raises(PlanError, match="missing required"):
        validate_plan(candidate)
    candidate = _refingerprint(
        template_plan,
        lambda data: data["spec"]["network"].pop("bridge"),
    )
    with pytest.raises(PlanError, match="missing required"):
        validate_plan(candidate)

    with pytest.raises(PlanError, match="not implemented"):
        _validate_operation_spec(unknown)
    with pytest.raises(PlanError, match="missing required"):
        _require_spec_keys({}, "one")


def test_evidence_validation_rejects_each_invalid_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition, plan, _template_plan, provider = _plans(tmp_path, monkeypatch)
    evidence = apply_vm_create(
        definition.safety,
        plan=plan,
        provider=provider,
        confirm=plan.metadata.plan_id,
    )

    cases = [
        (
            evidence.model_copy(
                update={
                    "metadata": evidence.metadata.model_copy(
                        update={"operation_kind": "unknown"}
                    )
                }
            ),
            "not implemented",
        ),
        (
            evidence.model_copy(
                update={
                    "provider": evidence.provider.model_copy(update={"name": "other"})
                }
            ),
            "provider mismatch",
        ),
        (
            evidence.model_copy(
                update={
                    "metadata": evidence.metadata.model_copy(update={"plan_id": ""})
                }
            ),
            "planId",
        ),
        (
            evidence.model_copy(
                update={
                    "steps": [
                        evidence.steps[0].model_copy(update={"id": ""}),
                        *evidence.steps[1:],
                    ]
                }
            ),
            "step id",
        ),
        (
            evidence.model_copy(
                update={"created_resources": [], "rollback": evidence.rollback}
            ),
            "createdResources",
        ),
        (
            evidence.model_copy(
                update={"plan": evidence.plan.model_copy(update={"snapshot": {}})}
            ),
            "snapshot is missing",
        ),
        (
            evidence.model_copy(
                update={
                    "plan": evidence.plan.model_copy(
                        update={"snapshot": {"kind": "bad"}}
                    )
                }
            ),
            "invalid evidence plan snapshot",
        ),
        (
            evidence.model_copy(
                update={
                    "metadata": evidence.metadata.model_copy(
                        update={"plan_id": "other"}
                    )
                }
            ),
            "different plan",
        ),
        (
            evidence.model_copy(
                update={
                    "metadata": evidence.metadata.model_copy(
                        update={"operation_kind": "proxmox.vm-template-create"}
                    )
                }
            ),
            "operation kind",
        ),
        (
            evidence.model_copy(
                update={
                    "metadata": evidence.metadata.model_copy(update={"target": "other"})
                }
            ),
            "target",
        ),
    ]
    for candidate, message in cases:
        with pytest.raises(PlanError, match=message):
            validate_evidence(candidate)


def test_remaining_parser_and_model_error_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition, plan, _template_plan, provider = _plans(tmp_path, monkeypatch)
    monkeypatch.setattr("sys.stdin", io.StringIO("[1]"))
    with pytest.raises(PlanError, match="JSON object"):
        read_artifact_arg("-")

    with pytest.raises(PlanError, match="metadata is missing"):
        canonical_plan_payload({})
    missing_fingerprint = plan.model_copy(
        update={
            "metadata": plan.metadata.model_copy(update={"fingerprint": None})
        }
    )
    with pytest.raises(PlanError, match="missing"):
        validate_fingerprint(missing_fingerprint)
    with pytest.raises(ProviderError, match="fingerprint is missing"):
        vm_evidence_plan(missing_fingerprint)
    with pytest.raises(ProviderError, match="fingerprint is missing"):
        template_evidence_plan(missing_fingerprint)

    invalid_plan = tmp_path / "invalid-plan.json"
    invalid_plan.write_text('{"kind": "OperationPlan"}', encoding="utf-8")
    with pytest.raises(PlanError, match="invalid operation plan"):
        load_plan(invalid_plan)
    invalid_evidence = tmp_path / "invalid-evidence.json"
    invalid_evidence.write_text('{"kind": "OperationEvidence"}', encoding="utf-8")
    with pytest.raises(PlanError, match="invalid operation evidence"):
        load_evidence(invalid_evidence)

    evidence = apply_vm_create(
        definition.safety,
        plan=plan,
        provider=provider,
        confirm=plan.metadata.plan_id,
    )
    data = evidence.as_artifact()
    data["metadata"]["createdAt"] = "2026-07-31T00:00:00"
    with pytest.raises(ValidationError, match="timezone"):
        OperationEvidence.model_validate(data)


def test_datetime_contract_requires_timezone() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        OperationPlan.model_validate(
            {
                "apiVersion": "atlas.operation/v1",
                "kind": "OperationPlan",
                "metadata": {
                    "planId": "plan",
                    "createdAt": "2026-07-31T00:00:00",
                    "operationKind": "proxmox.vm-create",
                    "target": "web01",
                    "site": "example",
                    "idempotencyKey": "key",
                },
                "source": {
                    "input": {"path": "/input", "digest": "sha256:x"},
                    "provider": {"path": "/provider", "digest": "sha256:x"},
                },
                "safety": {},
                "provider": {"name": "proxmox", "mode": "live", "node": "pve01"},
                "spec": {},
                "preflight": {"status": "failed"},
            }
        )
