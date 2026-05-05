from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import shutil
from pathlib import Path
import json
import os
import subprocess
import time
from typing import Any, cast

from .lock import file_lock
from .models import (
    MANIFEST_SCHEMA,
    Node,
    RuntimeState,
    load_yaml_config,
    load_yaml_file,
    utcnow,
)
from .indexer import write_command_index
from .release import activate_release
from .shims import generate_shims

_ALLOWED_FILE_PREFIXES = (
    Path("/etc"),
    Path("/opt"),
    Path("/usr/local"),
    Path("/var/lib/atlas"),
)


@dataclass
class RunResult:
    command: str
    code: int
    stdout: str
    stderr: str


def _load_command_index(active: Path) -> dict[str, dict[str, object]]:
    idx = active / "command-index.yml"
    if not idx.exists():
        return {}
    raw = load_yaml_file(idx)
    if (
        not isinstance(raw, dict)
        or set(raw.keys()) != {"commands"}
        or not isinstance(raw["commands"], dict)
    ):
        raise ValueError(
            "command-index.yml must have only a top-level 'commands' mapping"
        )
    normalized: dict[str, dict[str, object]] = {}
    raw_commands = raw["commands"]
    for name, val in raw_commands.items():
        entry = dict(val)
        normalized[name] = entry
    return normalized


def _check_allowed(
    meta: dict[str, object], node: Node, allow_destructive: bool
) -> None:
    pack = str(meta.get("pack", ""))
    if pack and pack not in node.packs:
        raise ValueError(f"pack not enabled for this host: {pack}")
    roles = cast(list[str], meta.get("allowed_roles", []) or [])
    if roles and node.role not in roles:
        raise ValueError(f"role not allowed: {node.role}")
    destructive = bool(meta.get("destructive", False))
    if destructive and not allow_destructive:
        raise ValueError("destructive command; pass --allow-destructive")


def _effective_timeout(requested: int, meta: dict[str, object]) -> int:
    header_limit = meta.get("timeout_sec")
    if header_limit is None:
        return requested
    limit = int(cast(Any, header_limit))
    return min(requested, limit)


def _redact(text: str, secrets: list[str]) -> str:
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "***REDACTED***")
    return redacted


def _append_run_record(
    logs: Path,
    *,
    command: str,
    args: list[str],
    caller: str,
    exit_code: int,
    duration_ms: int,
    release_version: str,
    node_name: str,
    node_role: str,
    pack: str,
    destructive: bool,
    stdout: str,
    stderr: str,
) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    base = f"{command}-{int(time.time() * 1000)}"
    stdout_path = logs / f"{base}.stdout.log"
    stderr_path = logs / f"{base}.stderr.log"
    stdout_path.write_text(stdout)
    stderr_path.write_text(stderr)
    record = {
        "timestamp": timestamp,
        "command": command,
        "args": args,
        "caller": caller,
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "release_version": release_version,
        "node_name": node_name,
        "node_role": node_role,
        "pack": pack,
        "destructive": destructive,
        "stdout_path": str(stdout_path.name),
        "stderr_path": str(stderr_path.name),
    }
    with (logs / "runs.jsonl").open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(record, ensure_ascii=False) + "\n")


def run_command(
    active: Path,
    locks: Path,
    logs: Path,
    etc: Path,
    name: str,
    args: list[str],
    timeout: int = 300,
    allow_destructive: bool = False,
    redact_values: list[str] | None = None,
) -> RunResult:
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
    effective_timeout = _effective_timeout(timeout, meta)
    started = time.perf_counter()
    lock_name = str(meta.get("lock", name))
    with file_lock(locks / f"{lock_name}.lock"):
        proc = subprocess.run(
            [str(script), *args],
            capture_output=True,
            text=True,
            timeout=effective_timeout,
            env=os.environ.copy(),
        )
    duration_ms = int((time.perf_counter() - started) * 1000)
    secrets = redact_values or []
    out = RunResult(
        name,
        proc.returncode,
        _redact(proc.stdout, secrets),
        _redact(proc.stderr, secrets),
    )
    (logs / f"{name}.log").write_text(
        f"exit={out.code}\nSTDOUT\n{out.stdout}\nSTDERR\n{out.stderr}\n"
    )
    state_root = Path(os.environ.get("ATLAS_VAR_DIR", "/var/lib/atlas"))
    state = RuntimeState.load(state_root / "state.yml")
    _append_run_record(
        logs,
        command=name,
        args=args,
        caller="atlas run",
        exit_code=out.code,
        duration_ms=duration_ms,
        release_version=state.current_version or "",
        node_name=node.name,
        node_role=node.role,
        pack=str(meta.get("pack", "")),
        destructive=bool(meta.get("destructive", False)),
        stdout=out.stdout,
        stderr=out.stderr,
    )
    return out


def _prepare_phase(release_dir: Path, staging_root: Path, version: str) -> Path:
    staged = staging_root / version
    if staged.exists():
        shutil.rmtree(staged)
    shutil.copytree(release_dir, staged, symlinks=True)
    return staged


def _validate_phase(staged: Path) -> None:
    idx = write_command_index(staged)
    raw = load_yaml_file(idx)
    if (
        not isinstance(raw, dict)
        or set(raw.keys()) != {"commands"}
        or not isinstance(raw["commands"], dict)
    ):
        raise ValueError(
            "command-index.yml must have only a top-level 'commands' mapping"
        )
    commands: dict[str, dict[str, object]] = raw["commands"]
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
            subprocess.run(
                ["systemd-analyze", "verify", str(unit)],
                check=True,
                capture_output=True,
                text=True,
            )


def _validate_files_path(rootfs_path: Path, rel: Path) -> None:
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"unsafe files path traversal: {rel}")
    for part in rel.parts:
        if part in {"", "."}:
            raise ValueError(f"unsafe files path segment: {rel}")
    resolved = rootfs_path.resolve(strict=False)
    if not any(
        resolved == p or p in resolved.parents or resolved in p.parents
        for p in _ALLOWED_FILE_PREFIXES
    ):
        raise ValueError(f"files target is outside allowed prefixes: {rootfs_path}")


def _copy_pack_files(staged: Path, node: Node) -> list[tuple[Path, Path]]:
    copied: list[tuple[Path, Path]] = []
    packs_root = staged / "packs"
    if not packs_root.exists():
        return copied
    for pack_name in node.packs:
        files_dir = packs_root / pack_name / "files"
        if not files_dir.exists():
            continue
        for src in files_dir.rglob("*"):
            rel = src.relative_to(files_dir)
            if src.is_symlink():
                raise ValueError(f"symlink traversal not allowed in pack files: {src}")
            rootfs_target = Path("/") / rel
            _validate_files_path(rootfs_target, rel)
            if src.is_dir():
                rootfs_target.mkdir(parents=True, exist_ok=True)
                continue
            rootfs_target.parent.mkdir(parents=True, exist_ok=True)
            backup = Path("")
            if rootfs_target.exists() or rootfs_target.is_symlink():
                backup = Path(f"{rootfs_target}.atlas-bak-{int(time.time() * 1000)}")
                if backup.exists():
                    backup.unlink()
                rootfs_target.rename(backup)
            shutil.copy2(src, rootfs_target)
            copied.append((rootfs_target, backup))
    return copied


def _restore_files(copied: list[tuple[Path, Path]]) -> None:
    for target, backup in reversed(copied):
        if target.exists() or target.is_symlink():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
        if backup:
            backup.rename(target)


def apply_release_with_phases(
    release_dir: Path,
    version: str,
    active_dir: Path,
    shims_dir: Path,
    libexec_dir: Path,
    state_path: Path,
    staging_root: Path,
    dry_run: bool = False,
) -> int:
    node = Node.load(Path(os.environ.get("ATLAS_ETC_DIR", "/etc/atlas")) / "node.yml")
    _ = load_yaml_config(release_dir / "manifest.yml", MANIFEST_SCHEMA)
    state = RuntimeState.load(state_path)
    old = state.current_version
    old_active_target = (
        active_dir.resolve()
        if (active_dir.exists() or active_dir.is_symlink())
        else None
    )

    staged = _prepare_phase(release_dir, staging_root, version)
    copied_files: list[tuple[Path, Path]] = []
    generated = 0

    try:
        _validate_phase(staged)
        write_command_index(release_dir)
        if not dry_run:
            activate_release(release_dir, active_dir)
            copied_files = _copy_pack_files(staged, node)
            generated = generate_shims(active_dir, shims_dir, libexec_dir)
        if not dry_run and shutil.which("systemctl"):
            try:
                subprocess.run(
                    ["systemctl", "daemon-reload"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except subprocess.CalledProcessError:
                pass

        if not dry_run:
            state.previous_version = old
            state.current_version = version
            state.last_apply_status = "success"
            state.last_apply_at = utcnow()
            state.save(state_path)
        return generated
    except Exception:
        _restore_files(copied_files)
        if old_active_target and old_active_target.exists():
            activate_release(old_active_target, active_dir)
        raise
    finally:
        if staged.exists():
            shutil.rmtree(staged)
