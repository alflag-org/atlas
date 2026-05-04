from __future__ import annotations

from dataclasses import dataclass
import shutil
from pathlib import Path
import json
import os
import subprocess

from .lock import file_lock
from .models import Node, RuntimeState, utcnow
from .indexer import write_command_index
from .release import activate_release
from .shims import generate_shims


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


def _prepare_phase(release_dir: Path, staging_root: Path, version: str) -> Path:
    staged = staging_root / version
    if staged.exists():
        shutil.rmtree(staged)
    shutil.copytree(release_dir, staged)
    return staged


def _validate_phase(staged: Path) -> None:
    idx = write_command_index(staged)
    commands: dict[str, dict[str, object]] = json.loads(idx.read_text())
    for name, meta in commands.items():
        rel_path = Path(str(meta.get("path", "")))
        target = (staged / rel_path).resolve()
        if not str(target).startswith(str(staged.resolve())):
            raise ValueError(f"unsafe command path for {name}: {rel_path}")
        if not target.exists():
            raise ValueError(f"command not found for {name}: {rel_path}")
        if not os.access(target, os.X_OK):
            raise ValueError(f"command not executable for {name}: {rel_path}")

    if shutil.which("systemd-analyze"):
        for unit in staged.rglob("*.service"):
            subprocess.run(["systemd-analyze", "verify", str(unit)], check=True, capture_output=True, text=True)


def apply_release_with_phases(
    release_dir: Path,
    version: str,
    active_dir: Path,
    shims_dir: Path,
    state_path: Path,
    staging_root: Path,
) -> int:
    state = RuntimeState.load(state_path)
    old = state.current_version
    staged = _prepare_phase(release_dir, staging_root, version)
    _validate_phase(staged)

    generated = 0
    write_command_index(release_dir)
    try:
        activate_release(release_dir, active_dir)
        generated = generate_shims(active_dir, shims_dir)
        state.previous_version = old
        state.current_version = version
        state.last_apply_status = "success"
        state.last_apply_at = utcnow()
        state.save(state_path)
    except Exception:
        if old:
            previous = release_dir.parent / old
            if previous.exists():
                activate_release(previous, active_dir)
        state.last_apply_status = "rollback"
        state.last_apply_at = utcnow()
        state.save(state_path)
        raise
    finally:
        if staged.exists():
            shutil.rmtree(staged)
    return generated
