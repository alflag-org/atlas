"""Strict ``release.yml`` parsing and executable artifact models."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .yamlutil import load_yaml_file

SCHEMA = "atlas.release/v1"
NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
RESERVED_COMMAND_NAMES = {"atlas", "artifact-runner"}
TARGET_RE = re.compile(
    r"^(?P<module>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*):"
    r"(?P<callable>[A-Za-z_][A-Za-z0-9_]*)$"
)


@dataclass(frozen=True)
class ExecutableArtifact:
    """One manifest-declared command or job."""

    name: str
    target: Target
    default_timeout_seconds: int | None = None


@dataclass(frozen=True)
class Target:
    """A release module and callable selected by a manifest entry."""

    module: str
    callable_name: str

    @property
    def spec(self) -> str:
        """Return the manifest form of the target."""
        return f"{self.module}:{self.callable_name}"


@dataclass(frozen=True)
class SystemdArtifacts:
    """Systemd files supplied for one service."""

    service: Path
    timer: Path | None = None


@dataclass(frozen=True)
class ServiceArtifact:
    """A logical service backed by one command or job."""

    name: str
    command: str | None
    job: str | None
    systemd: SystemdArtifacts


@dataclass(frozen=True)
class ReleaseManifest:
    """Validated artifact declarations for one release."""

    name: str
    commands: dict[str, ExecutableArtifact]
    jobs: dict[str, ExecutableArtifact]
    services: dict[str, ServiceArtifact]


def validate_name(name: str, *, kind: str = "artifact") -> str:
    """Validate a release or artifact identifier."""
    if not NAME_RE.fullmatch(name):
        raise ValueError(f"invalid {kind} name: {name}")
    if kind == "command" and name in RESERVED_COMMAND_NAMES:
        raise ValueError(f"reserved command name: {name}")
    return name


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be a mapping")
    return value


def _reject_unknown(raw: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"{label} has unknown key: {unknown[0]}")


def _required_string(raw: dict[str, Any], key: str, label: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}.{key} is required")
    return value.strip()


def _release_file(
    release_root: Path,
    value: str,
    label: str,
    *,
    suffix: str,
) -> Path:
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or "\\" in value:
        raise ValueError(f"{label} must be a relative path inside the release")
    path = release_root / relative
    current = path
    while current != release_root:
        if current.is_symlink():
            raise ValueError(f"{label} must not contain a symlink: {value}")
        current = current.parent
    resolved_root = release_root.resolve()
    resolved = path.resolve()
    if resolved_root not in resolved.parents:
        raise ValueError(f"{label} escapes the release root")
    if not path.is_file():
        raise ValueError(f"{label} not found: {value}")
    if path.suffix != suffix:
        raise ValueError(f"{label} must end with {suffix}")
    return resolved


def _module_source(release_root: Path, target: Target, label: str) -> Path:
    """Resolve a target module only below the selected release's modules root."""
    modules_root = release_root / "modules"
    if modules_root.is_symlink():
        raise ValueError(f"{label}.target must not contain a symlink: {target.spec}")
    if not modules_root.is_dir():
        raise ValueError(f"{label}.target module root not found: {modules_root}")
    module_parts = target.module.split(".")
    module_path = modules_root.joinpath(*module_parts)
    candidates = (module_path.with_suffix(".py"), module_path / "__init__.py")
    for candidate in candidates:
        current = candidate
        while current != modules_root:
            if current.is_symlink():
                raise ValueError(
                    f"{label}.target must not contain a symlink: {target.spec}"
                )
            current = current.parent
    existing = [candidate for candidate in candidates if candidate.is_file()]
    if len(existing) > 1:
        raise ValueError(f"{label}.target module is ambiguous: {target.module}")
    if not existing:
        raise ValueError(f"{label}.target module not found: {target.module}")
    source = existing[0].resolve()
    resolved_root = modules_root.resolve()
    if resolved_root not in source.parents:
        raise ValueError(
            f"{label}.target module escapes the release root: {target.module}"
        )
    return source


def _validate_target_callable(source: Path, target: Target, label: str) -> None:
    """Validate a target function without importing release code in Atlas."""
    try:
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        raise ValueError(f"{label}.target module cannot be parsed: {target.module}") from exc
    functions = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    if target.callable_name not in functions:
        raise ValueError(
            f"{label}.target callable is not a function: {target.callable_name}"
        )
    function = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == target.callable_name
    )
    if not isinstance(function, ast.FunctionDef):
        raise ValueError(  # noqa: TRY004 - manifest validation uses one error type
            f"{label}.target callable must be a synchronous function: "
            f"{target.callable_name}"
        )
    positional = [*function.args.posonlyargs, *function.args.args]
    if not positional and function.args.vararg is None:
        raise ValueError(
            f"{label}.target callable must accept argv: {target.callable_name}"
        )


def _parse_target(release_root: Path, value: Any, label: str) -> Target:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}.target is required")
    raw = value
    match = TARGET_RE.fullmatch(raw.strip())
    if match is None or raw != raw.strip():
        raise ValueError(f"{label}.target must be package.module:callable")
    target = Target(match.group("module"), match.group("callable"))
    source = _module_source(release_root, target, label)
    _validate_target_callable(source, target, label)
    return target


def _parse_executables(
    release_root: Path,
    raw: Any,
    *,
    kind: str,
) -> dict[str, ExecutableArtifact]:
    entries = _mapping(raw, kind + "s")
    parsed: dict[str, ExecutableArtifact] = {}
    allowed = {"target"}
    if kind == "job":
        allowed.add("default_timeout_seconds")
    for raw_name, raw_entry in entries.items():
        if not isinstance(raw_name, str):
            raise TypeError(f"{kind} name must be a string")
        name = validate_name(raw_name, kind=kind)
        label = f"{kind}s.{name}"
        entry = _mapping(raw_entry, label)
        _reject_unknown(entry, allowed, label)
        timeout = entry.get("default_timeout_seconds")
        if timeout is not None and (
            isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0
        ):
            raise ValueError(f"{label}.default_timeout_seconds must be a positive integer")
        parsed[name] = ExecutableArtifact(
            name=name,
            target=_parse_target(
                release_root,
                entry.get("target"),
                label,
            ),
            default_timeout_seconds=timeout,
        )
    return parsed


def _parse_services(
    release_root: Path,
    raw: Any,
    commands: dict[str, ExecutableArtifact],
    jobs: dict[str, ExecutableArtifact],
) -> dict[str, ServiceArtifact]:
    entries = _mapping(raw, "services")
    parsed: dict[str, ServiceArtifact] = {}
    for raw_name, raw_entry in entries.items():
        if not isinstance(raw_name, str):
            raise TypeError("service name must be a string")
        name = validate_name(raw_name, kind="service")
        label = f"services.{name}"
        entry = _mapping(raw_entry, label)
        _reject_unknown(entry, {"command", "job", "init"}, label)
        command = entry.get("command")
        job = entry.get("job")
        if (command is None) == (job is None):
            raise ValueError(f"{label} must reference exactly one command or job")
        if command is not None and (
            not isinstance(command, str) or command not in commands
        ):
            raise ValueError(f"{label}.command references an unknown command: {command}")
        if job is not None and (not isinstance(job, str) or job not in jobs):
            raise ValueError(f"{label}.job references an unknown job: {job}")

        init = _mapping(entry.get("init"), f"{label}.init")
        _reject_unknown(init, {"systemd"}, f"{label}.init")
        systemd = _mapping(init.get("systemd"), f"{label}.init.systemd")
        _reject_unknown(systemd, {"service", "timer"}, f"{label}.init.systemd")
        service_path = _release_file(
            release_root,
            _required_string(systemd, "service", f"{label}.init.systemd"),
            f"{label}.init.systemd.service",
            suffix=".service",
        )
        timer_value = systemd.get("timer")
        if timer_value is not None and (
            not isinstance(timer_value, str) or not timer_value.strip()
        ):
            raise ValueError(f"{label}.init.systemd.timer must be a non-empty string")
        timer_path = (
            None
            if timer_value is None
            else _release_file(
                release_root,
                timer_value.strip(),
                f"{label}.init.systemd.timer",
                suffix=".timer",
            )
        )
        parsed[name] = ServiceArtifact(
            name=name,
            command=command,
            job=job,
            systemd=SystemdArtifacts(service=service_path, timer=timer_path),
        )
    return parsed


def load_manifest(release_root: Path) -> ReleaseManifest:
    """Load and strictly validate ``release.yml`` from ``release_root``."""
    raw = _mapping(load_yaml_file(release_root / "release.yml"), "release.yml")
    _reject_unknown(
        raw,
        {"schema", "name", "commands", "jobs", "services"},
        "release.yml",
    )
    schema = _required_string(raw, "schema", "release.yml")
    if schema != SCHEMA:
        raise ValueError(f"unsupported release schema: {schema}")
    name = validate_name(_required_string(raw, "name", "release.yml"), kind="release")
    commands = _parse_executables(release_root, raw.get("commands", {}), kind="command")
    jobs = _parse_executables(release_root, raw.get("jobs", {}), kind="job")
    overlap = sorted(set(commands) & set(jobs))
    if overlap:
        raise ValueError(f"command and job names overlap: {overlap[0]}")
    services = _parse_services(
        release_root,
        raw.get("services", {}),
        commands,
        jobs,
    )
    return ReleaseManifest(
        name=name,
        commands=commands,
        jobs=jobs,
        services=services,
    )
