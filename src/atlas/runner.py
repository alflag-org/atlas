from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import time
import sys

from .paths import AtlasPaths
from .scripts import discover_commands, read_version


def _pythonpath(paths: AtlasPaths) -> str:
    base = [str(paths.home / "lib/python")]
    modules = paths.scripts / "modules"
    if modules.exists():
        base.insert(0, str(modules))
    existing = os.environ.get("PYTHONPATH")
    if existing:
        base.append(existing)
    return ":".join(base)


def _env(paths: AtlasPaths, command_name: str, version: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "ATLAS_HOME": str(paths.home),
            "ATLAS_ETC_DIR": str(paths.etc),
            "ATLAS_VAR_DIR": str(paths.var),
            "ATLAS_RUNTIME_DIR": str(paths.runtime),
            "ATLAS_SCRIPTS_DIR": str(paths.scripts),
            "ATLAS_HOST_FILE": str(paths.etc / "host.yml"),
            "ATLAS_SCRIPT_NAME": command_name,
            "ATLAS_SCRIPT_VERSION": version,
            "PYTHONPATH": _pythonpath(paths),
        }
    )
    return env


def _append_run_log(paths: AtlasPaths, name: str, args: list[str], version: str, exit_code: int, duration_ms: int) -> None:
    paths.logs.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "script": name,
        "args": args,
        "version": version,
        "exit_code": exit_code,
        "duration_ms": duration_ms,
    }
    with (paths.logs / "runs.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def resolve_command_path(scripts_root: Path, command_name: str) -> Path:
    commands = discover_commands(scripts_root / "commands")
    by_name = {entry.name: entry.script_path for entry in commands}
    if command_name not in by_name:
        raise ValueError(f"unknown command: {command_name}")
    return by_name[command_name]


def run_command(paths: AtlasPaths, command_name: str, args: list[str]) -> int:
    version = read_version(paths.scripts)
    command_path = resolve_command_path(paths.scripts, command_name)
    env = _env(paths, command_name, version)
    started = time.perf_counter()
    python_exe = paths.scripts_python if paths.scripts_python.exists() else Path(sys.executable)
    proc = subprocess.run(
        [str(python_exe), str(command_path), *args],
        env=env,
        text=True,
    )
    duration_ms = int((time.perf_counter() - started) * 1000)
    _append_run_log(paths, command_name, args, version, proc.returncode, duration_ms)
    return proc.returncode
