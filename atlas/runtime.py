from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
import subprocess

from .lock import file_lock
from .models import Node


@dataclass
class RunResult:
    command: str
    code: int
    stdout: str
    stderr: str


def _load_command_index(active: Path) -> dict[str, dict[str, object]]:
    idx = active / "command-index.json"
    if not idx.exists():
        return {}
    raw = json.loads(idx.read_text())
    normalized: dict[str, dict[str, object]] = {}
    for name, val in raw.items():
        if isinstance(val, str):
            normalized[name] = {"path": val, "pack": "", "destructive": False, "roles": []}
        else:
            normalized[name] = val
    return normalized


def _check_allowed(meta: dict[str, object], node: Node, allow_destructive: bool) -> None:
    pack = str(meta.get("pack", ""))
    if pack and pack not in node.packs:
        raise ValueError(f"pack not enabled for this host: {pack}")
    roles = meta.get("roles", []) or []
    if roles and node.role not in roles:
        raise ValueError(f"role not allowed: {node.role}")
    destructive = bool(meta.get("destructive", False))
    if destructive and not allow_destructive:
        raise ValueError("destructive command; pass --allow-destructive")


def run_command(active: Path, locks: Path, logs: Path, etc: Path, name: str, args: list[str], timeout: int = 300, allow_destructive: bool = False) -> RunResult:
    index = _load_command_index(active)
    if name not in index:
        raise ValueError(f"unknown command: {name}")
    meta = index[name]
    node = Node.load(etc / "node.yml")
    _check_allowed(meta, node, allow_destructive)

    script = active / str(meta["path"])
    if not script.exists():
        raise ValueError(f"script not found: {script}")

    logs.mkdir(parents=True, exist_ok=True)
    with file_lock(locks / f"{name}.lock"):
        proc = subprocess.run([str(script), *args], capture_output=True, text=True, timeout=timeout, env=os.environ.copy())
    out = RunResult(name, proc.returncode, proc.stdout, proc.stderr)
    (logs / f"{name}.log").write_text(f"exit={out.code}\nSTDOUT\n{out.stdout}\nSTDERR\n{out.stderr}\n")
    return out
