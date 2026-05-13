from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tarfile
from urllib.parse import urldefrag, urlparse
from urllib.request import urlopen
import zipfile

from .config import AtlasConfig


NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
RESERVED = {"atlas", "script-runner"}
ARCHIVE_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".zip")


@dataclass(frozen=True)
class CommandEntry:
    name: str
    script_path: Path


def _validate_segment(segment: str) -> None:
    if not NAME_RE.fullmatch(segment):
        raise ValueError(f"invalid command segment: {segment}")


def _command_name_from_rel_py(rel_py: Path) -> str:
    parts = list(rel_py.parts)
    stem = Path(parts[-1]).stem
    segments = [*parts[:-1], stem]
    for seg in segments:
        _validate_segment(seg)
    name = "-".join(segments)
    if "--" in name or name.endswith("-"):
        raise ValueError(f"invalid command name: {name}")
    if name in RESERVED:
        raise ValueError(f"reserved command name: {name}")
    if not NAME_RE.fullmatch(name):
        raise ValueError(f"invalid command name: {name}")
    return name


def discover_commands(commands_dir: Path) -> list[CommandEntry]:
    if not commands_dir.exists() or not commands_dir.is_dir():
        raise ValueError(f"commands directory not found: {commands_dir}")
    root = commands_dir.resolve()
    seen: dict[str, Path] = {}
    out: list[CommandEntry] = []
    for py_file in sorted(commands_dir.rglob("*.py")):
        if py_file.is_symlink():
            raise ValueError(f"symlink is not allowed: {py_file}")
        resolved = py_file.resolve()
        if root not in resolved.parents:
            raise ValueError(f"path traversal detected: {py_file}")
        rel = resolved.relative_to(root)
        name = _command_name_from_rel_py(rel)
        if name in seen:
            raise ValueError(f"command name conflict: {seen[name]} vs {py_file}")
        seen[name] = py_file
        out.append(CommandEntry(name=name, script_path=resolved))
    return out


def read_version(release_root: Path) -> str:
    version_file = release_root / "VERSION"
    if not version_file.exists():
        raise ValueError(f"missing VERSION file: {version_file}")
    version = version_file.read_text(encoding="utf-8").strip()
    if not version:
        raise ValueError("VERSION is empty")
    return version


def install_release(source: Path, releases_root: Path, current_link: Path) -> Path:
    if not source.exists() or not source.is_dir():
        raise ValueError(f"source directory not found: {source}")
    version = read_version(source)
    commands = source / "commands"
    discover_commands(commands)
    for item in source.rglob("*"):
        if item.is_symlink():
            raise ValueError(f"symlink is not allowed in scripts release: {item}")

    target = releases_root / version
    target.parent.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    staging = releases_root / f"{version}.tmp.{pid}"
    backup = releases_root / f"{version}.bak.{pid}"
    if staging.exists():
        shutil.rmtree(staging)
    if backup.exists():
        shutil.rmtree(backup)
    shutil.copytree(source, staging)

    replaced = False
    if target.exists():
        target.rename(backup)
        replaced = True
    try:
        staging.rename(target)
    except Exception:
        if replaced and backup.exists() and not target.exists():
            backup.rename(target)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if backup.exists():
            shutil.rmtree(backup)

    current_link.parent.mkdir(parents=True, exist_ok=True)
    tmp_link = current_link.parent / f"{current_link.name}.tmp.{pid}"
    if tmp_link.exists() or tmp_link.is_symlink():
        tmp_link.unlink()
    tmp_link.symlink_to(target, target_is_directory=True)
    tmp_link.replace(current_link)
    return target


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _cache_tmp(cache_dir: Path, kind: str) -> Path:
    root = cache_dir / "sources"
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f"{kind}.tmp.{os.getpid()}"
    if tmp.exists():
        _remove_path(tmp)
    return tmp


def _is_archive_name(name: str) -> bool:
    lowered = name.lower()
    return lowered.endswith(ARCHIVE_SUFFIXES)


def _safe_member_path(root: Path, name: str) -> Path:
    target = (root / name).resolve()
    root_resolved = root.resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise ValueError(f"archive path traversal detected: {name}")
    return target


def _validate_tar_member(root: Path, member: tarfile.TarInfo) -> None:
    if member.issym() or member.islnk():
        raise ValueError(f"archive link is not allowed: {member.name}")
    if member.name.startswith("/"):
        raise ValueError(f"archive absolute path is not allowed: {member.name}")
    _safe_member_path(root, member.name)


def _validate_zip_member(root: Path, member: zipfile.ZipInfo) -> None:
    if member.filename.startswith("/"):
        raise ValueError(f"archive absolute path is not allowed: {member.filename}")
    mode = member.external_attr >> 16
    if stat.S_IFMT(mode) == stat.S_IFLNK:
        raise ValueError(f"archive link is not allowed: {member.filename}")
    _safe_member_path(root, member.filename)


def _find_release_root(extracted_root: Path) -> Path:
    if (extracted_root / "VERSION").exists() and (extracted_root / "commands").is_dir():
        return extracted_root
    children = [entry for entry in extracted_root.iterdir() if entry.is_dir()]
    if len(children) == 1 and (children[0] / "VERSION").exists() and (children[0] / "commands").is_dir():
        return children[0]
    raise ValueError(f"archive does not contain an Atlas scripts release: {extracted_root}")


def _extract_archive(archive_path: Path, cache_dir: Path) -> Path:
    tmp = _cache_tmp(cache_dir, "archive")
    tmp.mkdir(parents=True, exist_ok=True)
    name = archive_path.name.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive_path) as zf:
            for member in zf.infolist():
                _validate_zip_member(tmp, member)
            zf.extractall(tmp)
    elif _is_archive_name(name):
        with tarfile.open(archive_path) as tf:
            for member in tf.getmembers():
                _validate_tar_member(tmp, member)
            tf.extractall(tmp, filter="data")
    else:
        raise ValueError(f"unsupported archive source: {archive_path}")
    return _find_release_root(tmp)


def _download_archive(source: str, cache_dir: Path) -> Path:
    tmp = _cache_tmp(cache_dir, "download")
    tmp.mkdir(parents=True, exist_ok=True)
    parsed = urlparse(source)
    archive_name = Path(parsed.path).name
    if not _is_archive_name(archive_name):
        raise ValueError(f"unsupported archive source: {source}")
    archive_path = tmp / archive_name
    with urlopen(source) as response:
        with archive_path.open("wb") as fh:
            shutil.copyfileobj(response, fh)
    return _extract_archive(archive_path, cache_dir)


def _clone_git(source: str, cache_dir: Path) -> Path:
    repo_url, ref = urldefrag(source.removeprefix("git+"))
    if not repo_url:
        raise ValueError("git source repository URL is required")
    tmp = _cache_tmp(cache_dir, "git")
    try:
        if ref:
            try:
                subprocess.run(["git", "clone", "--depth", "1", "--branch", ref, repo_url, str(tmp)], check=True)
            except subprocess.CalledProcessError:
                _remove_path(tmp)
                subprocess.run(["git", "clone", "--depth", "1", repo_url, str(tmp)], check=True)
                subprocess.run(["git", "-C", str(tmp), "fetch", "--depth", "1", "origin", ref], check=True)
                subprocess.run(["git", "-C", str(tmp), "checkout", "--detach", "FETCH_HEAD"], check=True)
        else:
            subprocess.run(["git", "clone", "--depth", "1", repo_url, str(tmp)], check=True)
    except FileNotFoundError as exc:
        raise ValueError("git command is required for git scripts source") from exc
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"git source clone failed: {repo_url}") from exc
    return tmp


def _resolve_registry(source: str, config: AtlasConfig | None, cache_dir: Path) -> Path | None:
    if config is None:
        return None
    entry = config.scripts.registries.get(source)
    if entry is None:
        return None
    return resolve_source(entry.source, config=config, cache_dir=cache_dir)


def resolve_source(source: str, *, config: AtlasConfig | None = None, cache_dir: Path | None = None) -> Path:
    source = source.strip()
    if not source:
        raise ValueError("scripts source is required")

    if cache_dir is not None:
        registry_path = _resolve_registry(source, config, cache_dir)
        if registry_path is not None:
            return registry_path

    if source.startswith("git+"):
        if cache_dir is None:
            raise ValueError("cache_dir is required for git scripts source")
        return _clone_git(source, cache_dir)

    parsed = urlparse(source)
    if parsed.scheme in {"http", "https"}:
        if cache_dir is None:
            raise ValueError("cache_dir is required for remote archive scripts source")
        return _download_archive(source, cache_dir)

    local = Path(source[7:]) if source.startswith("file://") else Path(source)
    if local.exists() and local.is_dir():
        return local
    if local.exists() and local.is_file() and _is_archive_name(local.name):
        if cache_dir is None:
            raise ValueError("cache_dir is required for archive scripts source")
        return _extract_archive(local, cache_dir)
    if local.exists():
        raise ValueError(f"unsupported scripts source: {source}")
    if cache_dir is not None and config is not None:
        raise ValueError(f"scripts source not found and registry alias is undefined: {source}")
    return local
