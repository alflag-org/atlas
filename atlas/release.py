from __future__ import annotations

from pathlib import Path
import hashlib
import tarfile


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_extract(tf: tarfile.TarFile, dest: Path) -> None:
    base = dest.resolve()
    for member in tf.getmembers():
        target = (dest / member.name).resolve()
        if not str(target).startswith(str(base)):
            raise ValueError(f"unsafe tar path detected: {member.name}")
        if member.uid < 0 or member.gid < 0 or member.uid > 65535 or member.gid > 65535:
            raise ValueError(f"unsafe tar ownership detected: {member.name}")
        mode = member.mode
        if mode < 0 or mode > 0o777:
            raise ValueError(f"unsafe tar mode detected: {member.name}")
    tf.extractall(dest)


def pull_bundle(bundle: Path, staged_dir: Path) -> dict:
    staged_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(bundle, "r:*") as tf:
        _safe_extract(tf, staged_dir)
    manifest = staged_dir / "manifest.yml"
    if not manifest.exists():
        raise ValueError("manifest.yml not found in bundle")
    from .models import load_yaml_file
    data = load_yaml_file(manifest)
    expected = data.get("checksum")
    payload = staged_dir / data["payload"]
    if expected and sha256_file(payload) != expected:
        raise ValueError("bundle checksum mismatch")
    with tarfile.open(payload, "r:*") as pt:
        _safe_extract(pt, staged_dir)
    return data


def activate_release(staged_dir: Path, active_dir: Path) -> None:
    if active_dir.exists() or active_dir.is_symlink():
        active_dir.unlink(missing_ok=True)
    active_dir.symlink_to(staged_dir.resolve(), target_is_directory=True)


def rollback_to(releases_root: Path, version: str, active_dir: Path) -> Path:
    source = releases_root / version
    if not source.exists():
        raise ValueError(f"unknown release: {version}")
    activate_release(source, active_dir)
    return source
