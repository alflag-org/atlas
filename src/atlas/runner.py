from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time
from datetime import datetime, timezone

from .paths import AtlasPaths
from .scriptsets import ReleaseCommand, active_releases, build_command_index


def _pythonpath(paths: AtlasPaths, command: ReleaseCommand) -> str:
    base = [str(paths.home / "lib/python")]
    module_paths: list[str] = []
    command_modules = command.release_root / "modules"
    if command_modules.exists():
        module_paths.append(str(command_modules))
    for release in active_releases(paths.scripts_current_root):
        if release.name == command.release_name:
            continue
        modules = release.root / "modules"
        if modules.exists():
            module_paths.append(str(modules))
    base = [*module_paths, *base]
    existing = os.environ.get("PYTHONPATH")
    if existing:
        base.append(existing)
    return ":".join(base)


def _env(paths: AtlasPaths, command: ReleaseCommand) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "ATLAS_HOME": str(paths.home),
            "ATLAS_ETC_DIR": str(paths.etc),
            "ATLAS_VAR_DIR": str(paths.var),
            "ATLAS_RUNTIME_DIR": str(paths.runtime),
            "ATLAS_SCRIPTS_DIR": str(command.release_root),
            "ATLAS_SCRIPTS_CURRENT_DIR": str(paths.scripts_current_root),
            "ATLAS_HOST_FILE": str(paths.etc / "host.yml"),
            "ATLAS_SCRIPT_NAME": command.name,
            "ATLAS_SCRIPT_VERSION": command.release_version,
            "ATLAS_SCRIPT_RELEASE_NAME": command.release_name,
            "PYTHONPATH": _pythonpath(paths, command),
        }
    )
    return env


def _append_run_log(
    paths: AtlasPaths,
    command: ReleaseCommand,
    args: list[str],
    exit_code: int,
    duration_ms: int,
) -> None:
    paths.logs.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "release": command.release_name,
        "script": command.name,
        "args": args,
        "version": command.release_version,
        "exit_code": exit_code,
        "duration_ms": duration_ms,
    }
    with (paths.logs / "runs.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def resolve_command(current_root: Path, command_name: str) -> ReleaseCommand:
    index = build_command_index(current_root)
    if command_name not in index:
        raise ValueError(f"unknown command: {command_name}")
    return index[command_name]


def resolve_command_path(current_root: Path, command_name: str) -> Path:
    return resolve_command(current_root, command_name).script_path


def run_command(paths: AtlasPaths, command_name: str, args: list[str]) -> int:
    command = resolve_command(paths.scripts_current_root, command_name)
    env = _env(paths, command)
    started = time.perf_counter()
    python_exe = paths.scripts_python
    if not python_exe.exists():
        raise ValueError(f"scripts python executable not found: {python_exe}")
    proc = subprocess.run(
        [str(python_exe), str(command.script_path), *args],
        env=env,
        text=True,
    )
    duration_ms = int((time.perf_counter() - started) * 1000)
    _append_run_log(paths, command, args, proc.returncode, duration_ms)
    return proc.returncode
