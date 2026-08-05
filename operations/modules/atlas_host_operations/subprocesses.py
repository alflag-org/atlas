"""Auditable child-process execution for host adapters and phase jobs."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from atlas_process_supervisor import ContainmentError, spawn_contained

_CONTAINMENT_EXIT_CODE = 125


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
            contained = spawn_contained(
                argv,
                cwd=cwd,
                env=child_env,
                stdin=subprocess.PIPE if input_text is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, stderr = contained.process.communicate(
                input=input_text,
                timeout=timeout_seconds,
            )
        except FileNotFoundError:
            return ChildResult(tuple(argv), 127, stderr=f"{argv[0]} command not found")
        except ContainmentError as exc:
            return ChildResult(
                tuple(argv),
                _CONTAINMENT_EXIT_CODE,
                stderr=f"process containment unavailable: {exc}",
            )
        except subprocess.TimeoutExpired as exc:
            try:
                contained.terminate()
            except ContainmentError as termination_error:
                stdout = exc.stdout
                stderr = exc.stderr
                error_message = termination_error
                try:
                    contained.cleanup()
                except ContainmentError as cleanup_error:
                    error_message = cleanup_error
                return ChildResult(
                    tuple(argv),
                    _CONTAINMENT_EXIT_CODE,
                    stdout=_timeout_text(stdout),
                    stderr=(
                        f"{_timeout_text(stderr)}\n"
                        f"process containment failed: {error_message}"
                    ).strip(),
                    timed_out=True,
                )
            stdout, stderr = contained.process.communicate()
            try:
                contained.cleanup()
            except ContainmentError as containment_error:
                return ChildResult(
                    tuple(argv),
                    _CONTAINMENT_EXIT_CODE,
                    stdout=_timeout_text(stdout),
                    stderr=(
                        f"{_timeout_text(stderr)}\n"
                        f"process containment failed: {containment_error}"
                    ).strip(),
                    timed_out=True,
                )
            return ChildResult(
                tuple(argv),
                124,
                stdout=_timeout_text(stdout if stdout is not None else exc.stdout),
                stderr=_timeout_text(stderr if stderr is not None else exc.stderr),
                timed_out=True,
            )
        try:
            contained.cleanup()
        except ContainmentError as exc:
            return ChildResult(
                tuple(argv),
                _CONTAINMENT_EXIT_CODE,
                stdout=stdout,
                stderr=(f"{_timeout_text(stderr)}\nprocess containment failed: {exc}").strip(),
            )
        if stderr:
            print(stderr.rstrip(), file=sys.stderr)
        return ChildResult(
            tuple(argv),
            contained.process.returncode,
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
