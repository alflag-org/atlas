"""Nested Atlas job execution for first-party release controllers."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

RELEASE_NAME = "operations"


def atlas_executable() -> str:
    """Return the host Atlas launcher selected for nested job execution."""
    configured = os.environ.get("ATLAS_EXECUTABLE")
    if configured:
        return configured
    return str(Path(os.environ.get("ATLAS_HOME", "/opt/atlas")) / "bin/atlas")


def job_argv(job: str, args: list[str]) -> list[str]:
    """Build exact argv for one private first-party job."""
    return [atlas_executable(), "job", "run", RELEASE_NAME, job, "--", *args]


def run_child(argv: list[str]) -> int:
    """Run one child with inherited cwd, environment, and streams."""
    try:
        process = subprocess.run(argv, check=False, shell=False)
    except FileNotFoundError:
        print(f"{argv[0]} command not found", file=sys.stderr)
        return 127
    return 128 + abs(process.returncode) if process.returncode < 0 else process.returncode


def run_job(job: str, args: list[str]) -> int:
    """Run one private job from the operations release."""
    return run_child(job_argv(job, args))
