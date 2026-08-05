"""Auditable child-process execution for host adapters and phase jobs."""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

_TERMINATE_GRACE_SECONDS = 5
_PR_SET_PDEATHSIG = 1


@dataclass(frozen=True)
class ChildResult:
    argv: tuple[str, ...]
    return_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


class CommandRunner(Protocol):
    def run(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int | None = None,
    ) -> ChildResult: ...


def _parent_death_preexec() -> object | None:
    """Return a Linux pre-exec hook that binds a child to this parent."""
    if sys.platform != "linux":
        return None
    parent_pid = os.getpid()

    def set_parent_death_signal() -> None:
        import ctypes

        libc = ctypes.CDLL(None)
        if libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0) != 0:
            os._exit(127)
        if os.getppid() != parent_pid:
            os.kill(os.getpid(), signal.SIGTERM)

    return set_parent_death_signal


def _process_groups_for_tree(root_pid: int) -> tuple[set[int], bool]:
    """Return process groups in a Linux descendant tree and whether it was read."""
    try:
        entries: dict[int, tuple[int, int]] = {}
        for entry in Path("/proc").iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                fields = (entry / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
                entries[int(entry.name)] = (int(fields[1]), int(fields[2]))
            except (OSError, ValueError, IndexError):
                continue
    except OSError:
        return {root_pid}, False

    if root_pid not in entries:
        return {root_pid}, False
    descendants = {root_pid}
    groups = {entries[root_pid][1]}
    changed = True
    while changed:
        changed = False
        for pid, (parent_pid, group_id) in entries.items():
            if pid in descendants or parent_pid not in descendants:
                continue
            descendants.add(pid)
            groups.add(group_id)
            changed = True
    return groups, len(descendants) > 1


def _signal_process_groups(groups: set[int], signum: int) -> None:
    for group_id in groups:
        try:
            os.killpg(group_id, signum)
        except ProcessLookupError:
            continue


def _groups_alive(groups: set[int]) -> bool:
    for group_id in groups:
        try:
            os.killpg(group_id, 0)
        except ProcessLookupError:
            continue
        return True
    return False


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    """Stop the child and any separately-created descendant process groups."""
    groups, has_descendants = _process_groups_for_tree(process.pid)
    if process.poll() is not None and not has_descendants:
        return
    _signal_process_groups(groups, signal.SIGTERM)
    try:
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    if has_descendants and process.poll() is not None and _groups_alive(groups):
        time.sleep(_TERMINATE_GRACE_SECONDS)
    if process.poll() is None or (has_descendants and _groups_alive(groups)):
        _signal_process_groups(groups, signal.SIGKILL)
        process.wait()


class SubprocessRunner:
    """Run one exact argv without exposing child stdout to artifact stdout."""

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int | None = None,
    ) -> ChildResult:
        print(f"$ {shlex.join(argv)}", file=sys.stderr)
        child_env = None
        if env is not None:
            child_env = os.environ.copy()
            child_env.update(env)
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=child_env,
                text=True,
                stdin=subprocess.PIPE if input_text is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                preexec_fn=_parent_death_preexec(),
                shell=False,
            )
            stdout, stderr = process.communicate(input=input_text, timeout=timeout_seconds)
        except FileNotFoundError:
            return ChildResult(tuple(argv), 127, stderr=f"{argv[0]} command not found")
        except subprocess.TimeoutExpired as exc:
            _terminate_process_group(process)
            stdout, stderr = process.communicate()
            return ChildResult(
                tuple(argv),
                124,
                stdout=_timeout_text(stdout if stdout is not None else exc.stdout),
                stderr=_timeout_text(stderr if stderr is not None else exc.stderr),
                timed_out=True,
            )
        if stderr:
            print(stderr.rstrip(), file=sys.stderr)
        return ChildResult(
            tuple(argv),
            process.returncode,
            stdout=stdout,
            stderr=stderr,
        )


@dataclass
class RecordingRunner:
    """Deterministic child runner for adapter contract tests."""

    results: list[ChildResult] = field(default_factory=list)
    calls: list[dict[str, object]] = field(default_factory=list)

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int | None = None,
    ) -> ChildResult:
        self.calls.append(
            {
                "argv": list(argv),
                "cwd": cwd,
                "input_text": input_text,
                "env": env,
                "timeout_seconds": timeout_seconds,
            }
        )
        if not self.results:
            raise AssertionError(f"no recorded result for command: {argv}")
        result = self.results.pop(0)
        return ChildResult(
            tuple(argv),
            result.return_code,
            stdout=result.stdout,
            stderr=result.stderr,
            timed_out=result.timed_out,
        )


def _timeout_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
