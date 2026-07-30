"""Validation and Ansible delegation for configuration commands."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

PLAYBOOK_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


def project_config(project_root: Path) -> Path:
    """Return a validated ``ansible.cfg`` in the current project."""
    config = project_root / "ansible.cfg"
    if not config.is_file() or config.is_symlink():
        raise ValueError(f"ansible.cfg not found in project root: {project_root}")
    return config


def playbook_path(project_root: Path, name: str) -> Path:
    """Resolve one safe playbook basename under ``playbooks``."""
    if not PLAYBOOK_RE.fullmatch(name):
        raise ValueError(f"invalid playbook name: {name}")
    playbook = project_root / "playbooks" / f"{name}.yml"
    if not playbook.is_file() or playbook.is_symlink():
        raise ValueError(f"playbook not found: {playbook}")
    return playbook


def inventory_path(project_root: Path, site: str) -> Path:
    """Resolve one safe site inventory under ``inventories``."""
    if not PLAYBOOK_RE.fullmatch(site):
        raise ValueError(f"invalid site name: {site}")
    inventory = project_root / "inventories" / site / "hosts.yml"
    if not inventory.is_file() or inventory.is_symlink():
        raise ValueError(f"inventory not found: {inventory}")
    return inventory


def target_name(value: str) -> str:
    """Reject an empty Ansible target while preserving its pattern."""
    if not value.strip():
        raise ValueError("target must not be empty")
    return value


def run_native(executable: str, args: list[str], project_root: Path) -> int:
    """Run one native tool with inherited streams and exact argv."""
    config = project_config(project_root)
    env = os.environ.copy()
    env["ANSIBLE_CONFIG"] = str(config)
    try:
        process = subprocess.run(
            [executable, *args],
            cwd=project_root,
            env=env,
            check=False,
            shell=False,
        )
    except FileNotFoundError:
        print(f"{executable} command not found", file=sys.stderr)
        return 127
    return process.returncode


def report_error(error: ValueError) -> int:
    """Print one validation diagnostic without a traceback."""
    print(str(error), file=sys.stderr)
    return 2
