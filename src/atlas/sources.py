from __future__ import annotations

from pathlib import Path
import os
import shutil
import stat
import subprocess
import tarfile
from urllib.parse import urldefrag, urlparse
from urllib.request import urlopen
import zipfile

from .config import AtlasConfig
from .files import remove_path


ARCHIVE_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".zip")
HTTP_TIMEOUT_SECONDS = 30


def is_archive_name(name: str) -> bool:
    return name.lower().endswith(ARCHIVE_SUFFIXES)


def _cache_tmp(cache_dir: Path, kind: str) -> Path:
    root = cache_dir / "sources"
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f"{kind}.tmp.{os.getpid()}"
    remove_path(tmp)
    return tmp


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


def extract_archive(archive_path: Path, cache_dir: Path) -> Path:
    tmp = _cache_tmp(cache_dir, "archive")
    tmp.mkdir(parents=True, exist_ok=True)
    name = archive_path.name.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive_path) as zf:
            for member in zf.infolist():
                _validate_zip_member(tmp, member)
            zf.extractall(tmp)
    elif is_archive_name(name):
        with tarfile.open(archive_path) as tf:
            for member in tf.getmembers():
                _validate_tar_member(tmp, member)
            if hasattr(tarfile, "data_filter"):
                tf.extractall(tmp, filter="data")
            else:
                tf.extractall(tmp)
    else:
        raise ValueError(f"unsupported archive source: {archive_path}")
    return _find_release_root(tmp)


def download_archive(source: str, cache_dir: Path) -> Path:
    tmp = _cache_tmp(cache_dir, "download")
    tmp.mkdir(parents=True, exist_ok=True)
    parsed = urlparse(source)
    archive_name = Path(parsed.path).name
    if not is_archive_name(archive_name):
        raise ValueError(f"unsupported archive source: {source}")
    archive_path = tmp / archive_name
    with urlopen(source, timeout=HTTP_TIMEOUT_SECONDS) as response:
        with archive_path.open("wb") as fh:
            shutil.copyfileobj(response, fh)
    return extract_archive(archive_path, cache_dir)


def clone_git_source(source: str, cache_dir: Path) -> Path:
    repo_url, ref = urldefrag(source.removeprefix("git+"))
    if not repo_url:
        raise ValueError("git source repository URL is required")
    tmp = _cache_tmp(cache_dir, "git")
    try:
        if ref:
            try:
                subprocess.run(["git", "clone", "--depth", "1", "--branch", ref, repo_url, str(tmp)], check=True)
            except subprocess.CalledProcessError:
                remove_path(tmp)
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
        return clone_git_source(source, cache_dir)

    parsed = urlparse(source)
    if parsed.scheme in {"http", "https"}:
        if cache_dir is None:
            raise ValueError("cache_dir is required for remote archive scripts source")
        return download_archive(source, cache_dir)

    local = Path(source[7:]) if source.startswith("file://") else Path(source)
    if local.exists() and local.is_dir():
        return local
    if local.exists() and local.is_file() and is_archive_name(local.name):
        if cache_dir is None:
            raise ValueError("cache_dir is required for archive scripts source")
        return extract_archive(local, cache_dir)
    if local.exists():
        raise ValueError(f"unsupported scripts source: {source}")
    if cache_dir is not None and config is not None:
        raise ValueError(f"scripts source not found and registry alias is undefined: {source}")
    return local
