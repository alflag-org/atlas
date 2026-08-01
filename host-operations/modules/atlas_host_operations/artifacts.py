"""Safe source loading, source binding, and host artifact fingerprints."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from atlas_host_operations.errors import InputError, PlanError
from atlas_host_operations.models import HostOperationPlan, HostSpec

_PLAINTEXT_SECRET_KEY = re.compile(
    r"(?:password|secret|token|credential|private[_-]?key)",
    re.IGNORECASE,
)
_SECRET_REF_PREFIXES = ("env:", "file:")


def safe_file(path: str | Path, *, base: Path | None = None) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        if base is None:
            raise InputError(f"source path must be absolute: {candidate}")
        candidate = base / candidate
    candidate = candidate.absolute()
    if not candidate.is_file() or candidate.is_symlink():
        raise InputError(f"source file not found or unsafe: {candidate}")
    return candidate


def safe_directory(path: str | Path, *, base: Path | None = None) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        if base is None:
            raise InputError(f"project path must be absolute: {candidate}")
        candidate = base / candidate
    candidate = candidate.absolute()
    if not candidate.is_dir() or candidate.is_symlink():
        raise InputError(f"project directory not found or unsafe: {candidate}")
    return candidate


def read_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise InputError(f"{path} is not valid YAML") from exc
    if not isinstance(value, dict):
        raise InputError(f"{path} must contain a YAML mapping")
    return value


def load_host_spec(path: str | Path) -> tuple[HostSpec, Path]:
    source = safe_file(path, base=Path.cwd())
    data = read_yaml(source)
    reject_plaintext_secrets(data)
    try:
        return HostSpec.model_validate(data), source
    except ValidationError as exc:
        raise InputError(f"host specification is invalid: {exc}") from exc


def file_digest(path: str | Path) -> str:
    source = Path(path)
    if not source.is_file() or source.is_symlink():
        raise InputError(f"digest source not found or unsafe: {source}")
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def canonical_plan_payload(
    plan_or_data: HostOperationPlan | dict[str, Any],
) -> dict[str, Any]:
    if isinstance(plan_or_data, HostOperationPlan):
        payload = plan_or_data.as_artifact(exclude_none=True)
    else:
        payload = copy.deepcopy(plan_or_data)
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        raise PlanError("plan metadata is missing")
    metadata.pop("fingerprint", None)
    return payload


def calculate_fingerprint(plan_or_data: HostOperationPlan | dict[str, Any]) -> str:
    canonical = json.dumps(
        canonical_plan_payload(plan_or_data),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def set_fingerprint(plan: HostOperationPlan) -> HostOperationPlan:
    data = plan.as_artifact(exclude_none=True)
    data["metadata"]["fingerprint"] = calculate_fingerprint(data)
    return HostOperationPlan.model_validate(data)


def validate_fingerprint(plan: HostOperationPlan) -> None:
    expected = calculate_fingerprint(plan)
    if plan.metadata.fingerprint != expected:
        message = (
            "plan fingerprint is invalid"
            if plan.metadata.fingerprint is not None
            else "plan fingerprint is missing"
        )
        raise PlanError(message)


def read_plan(path: str | None) -> HostOperationPlan:
    if path in (None, "-"):
        raw = sys.stdin.read()
        source = "<stdin>"
    else:
        plan_path = safe_file(path, base=Path.cwd())
        raw = plan_path.read_text(encoding="utf-8")
        source = str(plan_path)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PlanError(f"{source} is not valid JSON") from exc
    if not isinstance(data, dict):
        raise PlanError(f"{source} must contain a JSON object")
    try:
        plan = HostOperationPlan.model_validate(data)
    except ValidationError as exc:
        raise PlanError(f"host operation plan is invalid: {exc}") from exc
    validate_fingerprint(plan)
    return plan


def git_state(project_root: Path) -> tuple[str, bool]:
    def run(*args: str) -> str:
        try:
            process = subprocess.run(
                ["git", "-C", str(project_root), *args],
                check=True,
                capture_output=True,
                text=True,
                shell=False,
            )
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            raise InputError(
                f"project is not a readable Git checkout: {project_root}"
            ) from exc
        return process.stdout.strip()

    commit = run("rev-parse", "HEAD")
    dirty = bool(run("status", "--porcelain"))
    return commit, dirty


def validate_source_bindings(plan: HostOperationPlan) -> None:
    references = (
        ("host specification", plan.sources.host_spec),
        ("Registry profile", plan.sources.registry_profile),
        ("provider definition", plan.sources.provider_definition),
        ("provider input", plan.sources.provider_input),
    )
    for label, reference in references:
        path = safe_file(reference.path)
        if file_digest(path) != reference.digest:
            raise PlanError(f"{label} changed after planning")
    project = safe_directory(plan.sources.provisioning_project.path)
    commit, dirty = git_state(project)
    if commit != plan.sources.provisioning_project.git_commit:
        raise PlanError("provisioning project Git commit changed after planning")
    if dirty != plan.sources.provisioning_project.git_dirty:
        raise PlanError("provisioning project dirty state changed after planning")


def reject_plaintext_secrets(value: Any, *, key: str = "") -> None:
    if isinstance(value, dict):
        for child_key, child in value.items():
            reject_plaintext_secrets(child, key=str(child_key))
        return
    if isinstance(value, list):
        for child in value:
            reject_plaintext_secrets(child, key=key)
        return
    if isinstance(value, str) and _PLAINTEXT_SECRET_KEY.search(key):
        if not value.startswith(_SECRET_REF_PREFIXES):
            raise InputError(f"plaintext secret is forbidden at {key}")


def write_json(value: Any) -> None:
    if isinstance(value, BaseException):
        raise TypeError("exceptions cannot be written as artifacts")
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
