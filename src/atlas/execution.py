"""Shared command and job process execution."""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .catalog import ExecutableRef
from .locks import acquire_lock
from .paths import AtlasPaths

_SENSITIVE_TOKENS = ("password", "token", "secret", "key")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TIMEOUT_EXIT_CODE = 124
_TERMINATE_GRACE_SECONDS = 5


def redact_args(args: list[str]) -> list[str]:
    """Mask common secret-bearing argument forms before logging."""
    redacted: list[str] = []
    mask_next = False
    for arg in args:
        if mask_next:
            redacted.append("***")
            mask_next = False
            continue
        if arg.startswith("--"):
            key, separator, _ = arg.partition("=")
            normalized = key[2:].replace("-", "_").lower()
            sensitive = normalized.endswith("_token") or any(
                token in normalized for token in _SENSITIVE_TOKENS
            )
            if sensitive and separator:
                redacted.append(f"{key}=***")
                continue
            if sensitive:
                redacted.append(key)
                mask_next = True
                continue
        key, separator, _ = arg.partition("=")
        if separator and _ENV_NAME_RE.fullmatch(key):
            redacted.append(f"{key}=***")
            continue
        redacted.append(arg)
    return redacted


def _run_git(cwd: Path, args: list[str]) -> str | None:
    try:
        process = subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    return process.stdout.strip()


def git_context(cwd: Path) -> dict[str, str | bool | None]:
    """Collect read-only Git context when ``cwd`` belongs to a repository."""
    root = _run_git(cwd, ["rev-parse", "--show-toplevel"])
    if root is None:
        return {
            "git_root": None,
            "git_commit": None,
            "git_dirty": None,
            "git_branch": None,
        }
    branch = _run_git(cwd, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    status = _run_git(cwd, ["status", "--porcelain"])
    return {
        "git_root": root,
        "git_commit": _run_git(cwd, ["rev-parse", "HEAD"]),
        "git_dirty": None if status is None else bool(status),
        "git_branch": branch,
    }


def _parse_environment_file(path: Path) -> dict[str, str]:
    if not path.is_absolute():
        raise ValueError(f"environment file path must be absolute: {path}")
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"environment file not found: {path}")
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not _ENV_NAME_RE.fullmatch(key):
            raise ValueError(f"invalid environment assignment: {path}:{line_number}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _pythonpath(paths: AtlasPaths, executable: ExecutableRef, env: dict[str, str]) -> str:
    module_paths: list[str] = []
    selected_modules = executable.release.root / "modules"
    if selected_modules.is_dir():
        module_paths.append(str(selected_modules))
    module_paths.append(str(paths.home / "lib/python"))
    if env.get("PYTHONPATH"):
        module_paths.append(env["PYTHONPATH"])
    return os.pathsep.join(module_paths)


def _environment(
    paths: AtlasPaths,
    executable: ExecutableRef,
    *,
    run_id: str,
    parent_run_id: str | None,
    operation_id: str,
    environment_files: tuple[Path, ...],
) -> dict[str, str]:
    env = os.environ.copy()
    for path in environment_files:
        env.update(_parse_environment_file(path))
    for name in tuple(env):
        if name.startswith(("ATLAS_SCRIPT_", "ATLAS_SCRIPTS_")):
            env.pop(name)
    caller_path = env.get("PATH", "")
    path_parts = [str(paths.shims), str(paths.runtime_python.parent)]
    if caller_path:
        path_parts.append(caller_path)
    env.update(
        {
            "ATLAS_HOME": str(paths.home),
            "ATLAS_ETC_DIR": str(paths.etc),
            "ATLAS_VAR_DIR": str(paths.var),
            "ATLAS_RUNTIME_DIR": str(paths.runtime),
            "ATLAS_TMP_DIR": str(paths.tmp),
            "ATLAS_RELEASE_NAME": executable.release.name,
            "ATLAS_RELEASE_VERSION": executable.release.version,
            "ATLAS_ARTIFACT_TYPE": executable.artifact_type,
            "ATLAS_ARTIFACT_NAME": executable.artifact.name,
            "ATLAS_RELEASE_ROOT": str(executable.release.root),
            "ATLAS_HOST_FILE": str(paths.etc / "host.yml"),
            "ATLAS_RUN_ID": run_id,
            "ATLAS_PARENT_RUN_ID": parent_run_id or "",
            "ATLAS_OPERATION_ID": operation_id,
            "PATH": os.pathsep.join(path_parts),
            "PYTHONPATH": _pythonpath(paths, executable, env),
        }
    )
    return env


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        process.wait()


@contextmanager
def _forward_termination_signal(process: subprocess.Popen[str]) -> Iterator[None]:
    previous_handler = signal.getsignal(signal.SIGTERM)

    def forward(signum: int, _frame: object) -> None:
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGTERM, forward)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous_handler)


def _normalize_exit_code(return_code: int) -> int:
    return 128 + abs(return_code) if return_code < 0 else return_code


def _append_run_log(paths: AtlasPaths, record: dict[str, object]) -> None:
    if paths.logs.is_symlink() or (paths.logs.exists() and not paths.logs.is_dir()):
        raise ValueError(f"logs path must be a directory: {paths.logs}")
    paths.logs.mkdir(parents=True, exist_ok=True)
    path = paths.logs / "runs.jsonl"
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
    except OSError as exc:
        raise ValueError(f"run log must be a regular file: {path}") from exc
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ValueError(f"run log must be a regular file: {path}")
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _lock_context(paths: AtlasPaths, lock: str | None) -> Iterator[Path | None]:
    if lock is None:
        return nullcontext(None)
    return acquire_lock(paths.locks, lock)


def execute(
    paths: AtlasPaths,
    executable: ExecutableRef,
    args: list[str],
    *,
    cwd: Path | None = None,
    environment_files: tuple[Path, ...] = (),
    timeout_seconds: int | None = None,
    lock: str | None = None,
) -> int:
    """Execute an artifact, correlate it, and append one run record."""
    working_directory = Path.cwd() if cwd is None else cwd
    if not working_directory.is_absolute() or not working_directory.is_dir():
        raise ValueError(f"working directory not found: {working_directory}")
    if not paths.runtime_python.is_file():
        raise ValueError(f"runtime python executable not found: {paths.runtime_python}")
    if paths.release_runner.is_symlink() or not paths.release_runner.is_file():
        raise ValueError(f"release runner not found: {paths.release_runner}")
    if timeout_seconds is not None and (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout must be a positive integer")

    run_id = str(uuid4())
    parent_run_id = os.environ.get("ATLAS_RUN_ID") or None
    operation_id = os.environ.get("ATLAS_OPERATION_ID") or run_id
    env = _environment(
        paths,
        executable,
        run_id=run_id,
        parent_run_id=parent_run_id,
        operation_id=operation_id,
        environment_files=environment_files,
    )
    command = [
        str(paths.runtime_python),
        str(paths.release_runner),
        executable.artifact.target.spec,
        *args,
    ]
    display_args = redact_args(args)
    print(f"$ {shlex.join([executable.artifact.name, *display_args])}", file=sys.stderr)
    started_at = datetime.now(UTC)
    started = time.perf_counter()
    context = git_context(working_directory)
    timed_out = False
    interrupted = False
    with _lock_context(paths, lock):
        try:
            process = subprocess.Popen(
                command,
                cwd=working_directory,
                env=env,
                text=True,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise ValueError(f"runtime executable not found: {paths.runtime_python}") from exc
        with _forward_termination_signal(process):
            try:
                return_code = process.wait(timeout=timeout_seconds)
                exit_code = _normalize_exit_code(return_code)
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_process_group(process)
                exit_code = _TIMEOUT_EXIT_CODE
            except KeyboardInterrupt:
                interrupted = True
                _terminate_process_group(process)
                exit_code = 130

    duration_ms = int((time.perf_counter() - started) * 1000)
    record: dict[str, object] = {
        "run_id": run_id,
        "parent_run_id": parent_run_id,
        "operation_id": operation_id,
        "timestamp": started_at.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "release": executable.release.name,
        "artifact_type": executable.artifact_type,
        "artifact": executable.artifact.name,
        "args": redact_args(args),
        "version": executable.release.version,
        "cwd": str(working_directory),
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "timeout": timeout_seconds,
        "timed_out": timed_out,
        "lock": lock,
        **context,
    }
    _append_run_log(paths, record)
    if interrupted:
        raise KeyboardInterrupt
    return exit_code
