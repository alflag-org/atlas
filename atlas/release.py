from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

import yaml

from .indexer import write_command_index
from .models import load_yaml_file


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




SIGNATURE_FILE = "manifest.yml.minisig"


def trusted_minisign_pubkey_path() -> Path:
    etc_dir = Path(os.environ.get("ATLAS_ETC_DIR", "/etc/atlas"))
    return etc_dir / "trust.d" / "atlas-release.pub"


def verify_manifest_signature(staged_dir: Path) -> None:
    trusted_pubkey = trusted_minisign_pubkey_path()
    manifest = staged_dir / "manifest.yml"
    signature = staged_dir / SIGNATURE_FILE
    if not manifest.exists():
        raise ValueError("manifest.yml not found in bundle")
    if not signature.exists():
        raise ValueError(f"{SIGNATURE_FILE} not found in bundle")
    if not trusted_pubkey.exists():
        raise ValueError(f"trusted minisign public key not found: {trusted_pubkey}")

    cmd = [
        "minisign",
        "-Vm",
        str(manifest),
        "-x",
        str(signature),
        "-P",
        trusted_pubkey.read_text(encoding="utf-8").strip(),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise ValueError("minisign is required for signature verification") from e
    except subprocess.CalledProcessError as e:
        raise ValueError(f"manifest signature verification failed: {e.stderr.strip()}") from e
def build_bundle(release_dir: Path, bundle_path: Path, payload_name: str = "payload.tar.zst") -> Path:
    release_dir = release_dir.resolve()
    if not (release_dir / "packs").exists():
        raise ValueError(f"packs directory not found: {release_dir / 'packs'}")
    write_command_index(release_dir)

    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    payload_path = bundle_path.parent / payload_name
    with tarfile.open(payload_path, "w") as pt:
        for p in release_dir.iterdir():
            pt.add(p, arcname=p.name)

    checksum = sha256_file(payload_path)
    manifest = {"payload": payload_name, "checksum": checksum}
    manifest_path = bundle_path.parent / "manifest.yml"
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    with tarfile.open(bundle_path, "w") as tf:
        tf.add(manifest_path, arcname="manifest.yml")
        tf.add(payload_path, arcname=payload_name)

    payload_path.unlink(missing_ok=True)
    manifest_path.unlink(missing_ok=True)
    return bundle_path


def inspect_bundle(bundle: Path) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as td:
        staged = Path(td)
        with tarfile.open(bundle, "r:*") as tf:
            _safe_extract(tf, staged)
        verify_manifest_signature(staged)
        manifest_path = staged / "manifest.yml"
        manifest = load_yaml_file(manifest_path)
        payload = staged / manifest["payload"]
        with tarfile.open(payload, "r:*") as pt:
            names = pt.getnames()
        packs = sorted({Path(name).parts[1] for name in names if name.startswith("packs/") and len(Path(name).parts) > 1})
        return {
            "manifest": manifest,
            "payload": {
                "file": manifest["payload"],
                "size_bytes": payload.stat().st_size,
                "sha256": sha256_file(payload),
            },
            "packs": packs,
        }


def verify_bundle(bundle: Path) -> None:
    data = inspect_bundle(bundle)
    manifest = data["manifest"]
    expected = manifest.get("checksum")
    actual = data["payload"]["sha256"]
    if expected and expected != actual:
        raise SystemExit(f"payload checksum mismatch: expected={expected} actual={actual}")


def pull_bundle(bundle: Path, staged_dir: Path) -> dict:
    staged_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(bundle, "r:*") as tf:
        _safe_extract(tf, staged_dir)
    verify_manifest_signature(staged_dir)
    manifest = staged_dir / "manifest.yml"
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
