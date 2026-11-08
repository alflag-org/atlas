"""Construction of the JSON context passed to automation programs."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from atlas_core.host import get_host

from .catalog import CommandRef
from .environments import PERL_VARIABLES, perl_environment
from .paths import AtlasPaths


def base_context(paths: AtlasPaths) -> dict[str, object]:
    """Return host and path context for an Atlas diagnostic command."""
    host = get_host(paths.host_file)
    return {
        "version": 1,
        "host": host.to_dict(),
        "paths": paths.to_dict(),
        "program": None,
        "command": None,
        "execution": None,
        "working_directory": None,
    }


def execution_context(
    paths: AtlasPaths,
    command: CommandRef,
    *,
    run_id: str,
    parent_run_id: str | None,
    operation_id: str,
    working_directory: Path,
) -> dict[str, object]:
    """Build the context payload for one child process."""
    host = get_host(paths.host_file)
    program_runtime: dict[str, object] = {"type": command.program.runtime.type}
    if command.program.runtime.version is not None:
        program_runtime["version"] = command.program.runtime.version
    if command.program.runtime.environment is not None:
        program_runtime["environment"] = str(paths.environment(command.program.runtime.type, command.program.runtime.environment))
    return {
        "version": 1,
        "host": host.to_dict(),
        "paths": paths.to_dict(),
        "program": {
            "name": command.program.name,
            "root": str(command.program.root),
            "runtime": program_runtime,
        },
        "command": {
            "name": command.name,
            "path": str(command.path),
            "type": command.type,
        },
        "execution": {
            "run_id": run_id,
            "parent_run_id": parent_run_id,
            "operation_id": operation_id,
        },
        "working_directory": str(working_directory),
    }


def write_context(path: Path, payload: dict[str, object]) -> None:
    """Write one child context without following a symlink."""
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"context path must be a regular file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"context path must be a regular file: {path}")
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
    finally:
        if descriptor != -1:
            os.close(descriptor)


def child_environment(
    paths: AtlasPaths,
    command: CommandRef,
    payload: dict[str, object],
    *,
    context_file: Path,
    run_id: str,
    parent_run_id: str | None,
    operation_id: str,
    interpreter: Path | None,
    environment_path: Path | None,
) -> dict[str, str]:
    """Return the language-neutral environment exposed to a child."""
    environment = dict(os.environ)
    for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "ATLAS_ENVIRONMENT", "ATLAS_RUNTIME_VERSION", *PERL_VARIABLES):
        environment.pop(key, None)
    environment.update(
        {
            "ATLAS_CONTEXT_FILE": str(context_file),
            "ATLAS_HOME": str(paths.home),
            "ATLAS_ETC_DIR": str(paths.etc),
            "ATLAS_VAR_DIR": str(paths.var),
            "ATLAS_RUNTIMES_DIR": str(paths.runtimes),
            "ATLAS_ENVIRONMENTS_DIR": str(paths.environments),
            "ATLAS_SHIMS_DIR": str(paths.shims),
            "ATLAS_CONFIG_FILE": str(paths.config_file),
            "ATLAS_HOST_FILE": str(paths.host_file),
            "ATLAS_PROGRAM_NAME": command.program.name,
            "ATLAS_PROGRAM_ROOT": str(command.program.root),
            "ATLAS_COMMAND_NAME": command.name,
            "ATLAS_COMMAND_PATH": str(command.path),
            "ATLAS_RUNTIME_TYPE": command.type,
            "ATLAS_RUN_ID": run_id,
            "ATLAS_PARENT_RUN_ID": parent_run_id or "",
            "ATLAS_OPERATION_ID": operation_id,
        }
    )
    if command.program.runtime.version is not None:
        environment["ATLAS_RUNTIME_VERSION"] = command.program.runtime.version
    if environment_path is not None:
        environment["ATLAS_ENVIRONMENT"] = str(environment_path)
    if command.type == "python":
        assert environment_path is not None
        environment["VIRTUAL_ENV"] = str(environment_path)
        environment["PATH"] = os.pathsep.join([str(environment_path / "bin"), environment.get("PATH", "")])
        modules = command.program.root / "modules"
        roots = [command.program.root]
        if modules.is_dir():
            roots.insert(0, modules)
        environment["PYTHONPATH"] = os.pathsep.join(str(root) for root in roots)
    elif command.type == "perl":
        assert interpreter is not None and environment_path is not None
        environment.update(perl_environment(interpreter, environment_path))
        modules = command.program.root / "lib"
        environment["PERL5LIB"] = os.pathsep.join([str(modules), environment["PERL5LIB"]])
    return environment
