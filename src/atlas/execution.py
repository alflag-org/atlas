"""Shared command and job process execution."""

from __future__ import annotations

import json
import os
import re
import select
import shlex
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .catalog import (
    ActiveRelease,
    ExecutableRef,
    release_from_snapshot,
    resolve_command,
    resolve_command_from_release,
)
from .generations import _generation_lease_handoff, collect_generation_garbage
from .launchers import active_artifact_generation
from .locks import acquire_lock
from .paths import AtlasPaths
from .runtime import active_runtime_generation

_SENSITIVE_TOKENS = ("password", "token", "secret", "key")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TIMEOUT_EXIT_CODE = 124
_TERMINATE_GRACE_SECONDS = 5
_LEASE_HANDOFF_TIMEOUT_SECONDS = 5
_LEASE_HANDOFF_ENVIRONMENT_KEYS = (
    "ATLAS_LEASE_HANDOFF_RUNTIME_FD",
    "ATLAS_LEASE_HANDOFF_ARTIFACT_FD",
    "ATLAS_LEASE_HANDOFF_ACK_FD",
)


@dataclass(frozen=True)
class _ExecutionSnapshot:
    """Concrete runtime and artifact paths selected for one child."""

    runtime_generation: Path
    runtime_python: Path
    artifact_generation: Path
    release_runner: Path


@dataclass(frozen=True)
class PinnedExecution:
    """Immutable release and generation selection inherited by nested work."""

    release: ActiveRelease
    runtime_generation: Path
    artifact_generation: Path


_PINNED_EXECUTION_ENVIRONMENT_KEYS = (
    "ATLAS_EXECUTION_RELEASE_NAME",
    "ATLAS_EXECUTION_RELEASE_VERSION",
    "ATLAS_EXECUTION_RELEASE_DIGEST",
    "ATLAS_EXECUTION_RELEASE_ROOT",
    "ATLAS_EXECUTION_RUNTIME_GENERATION",
    "ATLAS_EXECUTION_ARTIFACT_GENERATION",
)


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


def _pythonpath(
    paths: AtlasPaths,
    executable: ExecutableRef,
    env: dict[str, str],
    *,
    artifact_python: Path | None = None,
) -> str:
    module_paths: list[str] = []
    selected_modules = executable.release.root / "modules"
    if selected_modules.is_dir():
        module_paths.append(str(selected_modules))
    module_paths.append(str(artifact_python or paths.home / "lib/python"))
    return os.pathsep.join(module_paths)


def _environment(
    paths: AtlasPaths,
    executable: ExecutableRef,
    *,
    run_id: str,
    parent_run_id: str | None,
    operation_id: str,
    environment_files: tuple[Path, ...],
    runtime_generation: Path | None = None,
    artifact_generation: Path | None = None,
) -> dict[str, str]:
    env = os.environ.copy()
    for key in _LEASE_HANDOFF_ENVIRONMENT_KEYS:
        env.pop(key, None)
    for path in environment_files:
        env.update(_parse_environment_file(path))
    for name in tuple(env):
        if name.startswith(("ATLAS_SCRIPT_", "ATLAS_SCRIPTS_")):
            env.pop(name)
    caller_path = env.get("PATH", "")
    shims = artifact_generation / "shims" if artifact_generation is not None else paths.shims
    runtime_bin = (
        runtime_generation / "bin" if runtime_generation is not None else paths.runtime_python.parent
    )
    artifact_python = artifact_generation / "python" if artifact_generation is not None else None
    path_parts = [str(shims), str(runtime_bin)]
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
            "ATLAS_RELEASE_DIGEST": executable.release.content_digest,
            "ATLAS_RUNTIME_GENERATION": str(runtime_generation or paths.runtime_python.parent.parent),
            "ATLAS_ARTIFACT_GENERATION": str(
                artifact_generation or paths.artifact_current.resolve()
            ),
            "ATLAS_HOST_FILE": str(paths.etc / "host.yml"),
            "ATLAS_RUN_ID": run_id,
            "ATLAS_PARENT_RUN_ID": parent_run_id or "",
            "ATLAS_OPERATION_ID": operation_id,
            "ATLAS_EXECUTION_RELEASE_NAME": executable.release.name,
            "ATLAS_EXECUTION_RELEASE_VERSION": executable.release.version,
            "ATLAS_EXECUTION_RELEASE_DIGEST": executable.release.content_digest,
            "ATLAS_EXECUTION_RELEASE_ROOT": str(executable.release.root),
            "ATLAS_EXECUTION_RUNTIME_GENERATION": str(
                runtime_generation or paths.runtime_python.parent.parent
            ),
            "ATLAS_EXECUTION_ARTIFACT_GENERATION": str(
                artifact_generation or paths.artifact_current.resolve()
            ),
            "PATH": os.pathsep.join(path_parts),
            "PYTHONPATH": _pythonpath(
                paths,
                executable,
                env,
                artifact_python=artifact_python,
            ),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return env


def _validated_generation(
    path: Path,
    generations: Path,
    *,
    label: str,
) -> Path:
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise ValueError(f"selected {label} is not a concrete generation: {path}")
    if path.parent.resolve() != generations.resolve():
        raise ValueError(f"selected {label} is outside its generations directory: {path}")
    return path


def pinned_execution_selection(paths: AtlasPaths) -> PinnedExecution | None:
    """Read and validate the internal immutable selection, if one was inherited."""
    values = {key: os.environ.get(key) for key in _PINNED_EXECUTION_ENVIRONMENT_KEYS}
    present = [value is not None for value in values.values()]
    if not any(present):
        return None
    if not all(present):
        raise ValueError("nested execution selection is incomplete")
    assert all(value is not None for value in values.values())
    release = release_from_snapshot(
        Path(values["ATLAS_EXECUTION_RELEASE_ROOT"]),
        paths.releases_root,
        expected_name=values["ATLAS_EXECUTION_RELEASE_NAME"],
        expected_version=values["ATLAS_EXECUTION_RELEASE_VERSION"],
        expected_digest=values["ATLAS_EXECUTION_RELEASE_DIGEST"],
    )
    runtime_generation = _validated_generation(
        Path(values["ATLAS_EXECUTION_RUNTIME_GENERATION"]),
        paths.runtime / "python/envs/generations",
        label="runtime generation",
    )
    artifact_generation = _validated_generation(
        Path(values["ATLAS_EXECUTION_ARTIFACT_GENERATION"]),
        paths.artifact_root / "generations",
        label="artifact generation",
    )
    return PinnedExecution(
        release=release,
        runtime_generation=runtime_generation,
        artifact_generation=artifact_generation,
    )


def resolve_command_for_execution(paths: AtlasPaths, name: str) -> ExecutableRef:
    """Resolve a command from the inherited snapshot or current selection."""
    pinned = pinned_execution_selection(paths)
    if pinned is not None:
        return resolve_command_from_release(pinned.release, name)
    return resolve_command(paths.current_root, paths.releases_root, name)


def _capture_generation_snapshot(
    paths: AtlasPaths,
    selected: ExecutableRef,
    pinned: PinnedExecution | None,
) -> _ExecutionSnapshot:
    if pinned is None:
        runtime_generation = active_runtime_generation(paths.runtime)
        artifact_generation = active_artifact_generation(paths)
    else:
        if selected.release != pinned.release:
            raise ValueError("nested execution release selection changed")
        runtime_generation = pinned.runtime_generation
        artifact_generation = pinned.artifact_generation
    runtime_python = runtime_generation / "bin/python"
    release_runner = artifact_generation / "python/atlas_release_runner.py"
    if not runtime_python.is_file():
        raise ValueError(f"runtime python executable not found: {paths.runtime_python}")
    if not release_runner.is_file():
        raise ValueError(f"release runner not found: {paths.release_runner}")
    return _ExecutionSnapshot(
        runtime_generation=runtime_generation,
        runtime_python=runtime_python,
        artifact_generation=artifact_generation,
        release_runner=release_runner,
    )


def _selected_executable(
    executable: ExecutableRef | Callable[[], ExecutableRef],
) -> ExecutableRef:
    selected = executable() if callable(executable) else executable
    if not isinstance(selected, ExecutableRef):
        raise TypeError("executable resolver must return an ExecutableRef")
    return selected


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


def _await_generation_lease_ack(process: subprocess.Popen[str], ack_fd: int) -> None:
    """Wait until the child owns both generations before releasing parent leases."""
    try:
        ready, _, _ = select.select([ack_fd], [], [], _LEASE_HANDOFF_TIMEOUT_SECONDS)
    except OSError as exc:
        _terminate_process_group(process)
        raise ValueError("release child lease handoff failed") from exc
    if not ready:
        _terminate_process_group(process)
        raise ValueError("release child did not acknowledge generation leases")
    try:
        acknowledged = os.read(ack_fd, 1) == b"1"
    except OSError as exc:
        _terminate_process_group(process)
        raise ValueError("release child lease handoff failed") from exc
    if not acknowledged:
        _terminate_process_group(process)
        raise ValueError("release child rejected generation lease handoff")


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
    executable: ExecutableRef | Callable[[], ExecutableRef],
    args: list[str],
    *,
    cwd: Path | None = None,
    environment_files: tuple[Path, ...] = (),
    timeout_seconds: int | Callable[[ExecutableRef], int | None] | None = None,
    lock: str | None = None,
) -> int:
    """Execute an artifact, correlate it, and append one run record."""
    working_directory = Path.cwd() if cwd is None else cwd
    if not working_directory.is_absolute() or not working_directory.is_dir():
        raise ValueError(f"working directory not found: {working_directory}")
    run_id = str(uuid4())
    parent_run_id = os.environ.get("ATLAS_RUN_ID") or None
    operation_id = os.environ.get("ATLAS_OPERATION_ID") or run_id
    started_at = datetime.now(UTC)
    started = time.perf_counter()
    context = git_context(working_directory)
    timed_out = False
    interrupted = False
    process: subprocess.Popen[str] | None = None
    spawn_contexts = ExitStack()
    try:
        # Activation uses this lock before any release locks. The lock is held
        # only while resolving and spawning so a running child does not block a
        # later release publication.
        with acquire_lock(paths.locks, "host-artifacts", wait=True):
            selected = _selected_executable(executable)
            selected_timeout = (
                timeout_seconds(selected)
                if callable(timeout_seconds)
                else timeout_seconds
            )
            if selected_timeout is not None and (
                isinstance(selected_timeout, bool)
                or not isinstance(selected_timeout, int)
                or selected_timeout <= 0
            ):
                raise ValueError("timeout must be a positive integer")
            pinned = pinned_execution_selection(paths)
            snapshot = _capture_generation_snapshot(paths, selected, pinned)
            env = _environment(
                paths,
                selected,
                run_id=run_id,
                parent_run_id=parent_run_id,
                operation_id=operation_id,
                environment_files=environment_files,
                runtime_generation=snapshot.runtime_generation,
                artifact_generation=snapshot.artifact_generation,
            )
            command = [
                str(snapshot.runtime_python),
                str(snapshot.release_runner),
                selected.artifact.target.spec,
                *args,
            ]
            display_args = redact_args(args)
            print(
                f"$ {shlex.join([selected.artifact.name, *display_args])}",
                file=sys.stderr,
            )
            spawn_contexts.enter_context(_lock_context(paths, lock))
            with _generation_lease_handoff(
                snapshot.runtime_generation.parent,
                snapshot.runtime_generation,
                snapshot.artifact_generation.parent,
                snapshot.artifact_generation,
            ) as handoff:
                ack_read = ack_write = -1
                try:
                    ack_read, ack_write = os.pipe2(os.O_CLOEXEC)
                    env.update(
                        {
                            "ATLAS_LEASE_HANDOFF_RUNTIME_FD": str(handoff.runtime_fd),
                            "ATLAS_LEASE_HANDOFF_ARTIFACT_FD": str(handoff.artifact_fd),
                            "ATLAS_LEASE_HANDOFF_ACK_FD": str(ack_write),
                        }
                    )
                    try:
                        process = subprocess.Popen(
                            command,
                            cwd=working_directory,
                            env=env,
                            text=True,
                            start_new_session=True,
                            pass_fds=(handoff.runtime_fd, handoff.artifact_fd, ack_write),
                        )
                    except FileNotFoundError as exc:
                        raise ValueError(
                            f"runtime executable not found: {paths.runtime_python}"
                        ) from exc
                    os.close(ack_write)
                    ack_write = -1
                    _await_generation_lease_ack(process, ack_read)
                finally:
                    if ack_write >= 0:
                        os.close(ack_write)
                    if ack_read >= 0:
                        os.close(ack_read)

        assert process is not None
        with _forward_termination_signal(process):
            try:
                return_code = process.wait(timeout=selected_timeout)
                exit_code = _normalize_exit_code(return_code)
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_process_group(process)
                exit_code = _TIMEOUT_EXIT_CODE
            except KeyboardInterrupt:
                interrupted = True
                _terminate_process_group(process)
                exit_code = 130
    finally:
        spawn_contexts.close()

    duration_ms = int((time.perf_counter() - started) * 1000)
    record: dict[str, object] = {
        "run_id": run_id,
        "parent_run_id": parent_run_id,
        "operation_id": operation_id,
        "timestamp": started_at.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "release": selected.release.name,
        "artifact_type": selected.artifact_type,
        "artifact": selected.artifact.name,
        "args": redact_args(args),
        "version": selected.release.version,
        "release_digest": selected.release.content_digest,
        "cwd": str(working_directory),
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "timeout": selected_timeout,
        "timed_out": timed_out,
        "lock": lock,
        **context,
    }
    _append_run_log(paths, record)
    with acquire_lock(paths.locks, "host-artifacts", wait=True):
        collect_generation_garbage(
            snapshot.runtime_generation.parent,
            paths.runtime / "python/envs/scripts",
            label="runtime generation",
        )
        collect_generation_garbage(
            snapshot.artifact_generation.parent,
            paths.artifact_current,
            label="artifact generation",
        )
    if interrupted:
        raise KeyboardInterrupt
    return exit_code
