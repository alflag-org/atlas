"""Auditable child-process execution for host adapters and phase jobs."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


def atlas_executable() -> str:
    """Return the Atlas launcher selected for nested private jobs."""
    configured = os.environ.get("ATLAS_EXECUTABLE")
    if configured:
        return configured
    return str(Path(os.environ.get("ATLAS_HOME", "/opt/atlas")) / "bin/atlas")


def job_argv(release: str, job: str, args: list[str]) -> list[str]:
    """Build exact argv for a private job without crossing a public CLI."""
    return [atlas_executable(), "job", "run", release, job, "--", *args]


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
            process = subprocess.run(
                argv,
                cwd=cwd,
                env=child_env,
                input=input_text,
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
                shell=False,
            )
        except FileNotFoundError:
            return ChildResult(tuple(argv), 127, stderr=f"{argv[0]} command not found")
        except subprocess.TimeoutExpired as exc:
            return ChildResult(
                tuple(argv),
                124,
                stdout=_timeout_text(exc.stdout),
                stderr=_timeout_text(exc.stderr),
                timed_out=True,
            )
        if process.stderr:
            print(process.stderr.rstrip(), file=sys.stderr)
        return ChildResult(
            tuple(argv),
            process.returncode,
            stdout=process.stdout,
            stderr=process.stderr,
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
