from __future__ import annotations

import io
import os
from pathlib import Path
import stat
import subprocess
import tarfile
import warnings
import zipfile

import pytest

from atlas.sources import (
    clone_git_source,
    download_archive,
    extract_archive,
    is_archive_name,
    resolve_source,
)


def test_resolve_local_directory_and_file_url(release_factory) -> None:
    release = release_factory()
    assert resolve_source(str(release)) == release
    assert resolve_source(f"file://{release}") == release
    assert is_archive_name("release.TAR.GZ") is True
    assert is_archive_name("release.bin") is False


def test_resolve_tar_and_zip_archives(release_factory, tmp_path: Path) -> None:
    release = release_factory()
    tar_path = tmp_path / "release.tar.gz"
    with tarfile.open(tar_path, "w:gz") as archive:
        archive.add(release, arcname="release")
    tar_root = resolve_source(str(tar_path), cache_dir=tmp_path / "cache")
    assert (tar_root / "release.yml").is_file()

    zip_path = tmp_path / "release.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        for item in release.rglob("*"):
            archive.write(item, item.relative_to(release.parent))
    zip_root = resolve_source(str(zip_path), cache_dir=tmp_path / "cache")
    assert (zip_root / "VERSION").is_file()


def test_archive_at_root_and_stale_cache_are_supported(release_factory, tmp_path: Path) -> None:
    release = release_factory()
    archive_path = tmp_path / "root.tar"
    with tarfile.open(archive_path, "w") as archive:
        for item in release.rglob("*"):
            archive.add(item, arcname=str(item.relative_to(release)))
    stale = tmp_path / "cache/sources" / f"archive.tmp.{os.getpid()}"
    stale.mkdir(parents=True)
    (stale / "stale").write_text("x", encoding="utf-8")

    root = extract_archive(archive_path, tmp_path / "cache")

    assert root == stale
    assert not (root / "stale").exists()
    assert (root / "release.yml").exists()


def test_archive_rejects_traversal_absolute_and_links(tmp_path: Path) -> None:
    traversal = tmp_path / "traversal.tar"
    with tarfile.open(traversal, "w") as archive:
        payload = b"x"
        info = tarfile.TarInfo("../escape")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    with pytest.raises(ValueError, match="path traversal"):
        extract_archive(traversal, tmp_path / "cache")

    absolute = tmp_path / "absolute.tar"
    with tarfile.open(absolute, "w") as archive:
        archive.addfile(tarfile.TarInfo("/escape"))
    with pytest.raises(ValueError, match="absolute path"):
        extract_archive(absolute, tmp_path / "cache")

    link = tmp_path / "link.tar"
    with tarfile.open(link, "w") as archive:
        info = tarfile.TarInfo("release/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        archive.addfile(info)
    with pytest.raises(ValueError, match="link is not allowed"):
        extract_archive(link, tmp_path / "cache")

    zip_absolute = tmp_path / "absolute.zip"
    with zipfile.ZipFile(zip_absolute, "w") as archive:
        archive.writestr("/escape", "")
    with pytest.raises(ValueError, match="absolute path"):
        extract_archive(zip_absolute, tmp_path / "cache")

    zip_link = tmp_path / "link.zip"
    info = zipfile.ZipInfo("release/link")
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(zip_link, "w") as archive:
        archive.writestr(info, "target")
    with pytest.raises(ValueError, match="link is not allowed"):
        extract_archive(zip_link, tmp_path / "cache")


def test_archive_requires_one_release_root(tmp_path: Path) -> None:
    archive_path = tmp_path / "bad.tar"
    with tarfile.open(archive_path, "w") as archive:
        archive.addfile(tarfile.TarInfo("README.md"))
    with pytest.raises(ValueError, match="does not contain an Atlas release"):
        extract_archive(archive_path, tmp_path / "cache")

    unsupported = tmp_path / "release.rar"
    unsupported.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported archive source"):
        extract_archive(unsupported, tmp_path / "cache")


def test_tar_extraction_fallback_without_data_filter(
    release_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = release_factory()
    archive_path = tmp_path / "release.tar"
    with tarfile.open(archive_path, "w") as archive:
        archive.add(release, arcname="release")
    if hasattr(tarfile, "data_filter"):
        monkeypatch.delattr(tarfile, "data_filter")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        assert extract_archive(archive_path, tmp_path / "cache").name == "release"


def test_download_http_archive(
    release_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = release_factory()
    archive_path = tmp_path / "release.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(release, arcname="release")
    data = archive_path.read_bytes()

    def fake_urlopen(source: str, timeout: int):
        assert source == "https://example.test/release.tar.gz"
        assert timeout == 30
        return io.BytesIO(data)

    monkeypatch.setattr("atlas.sources.urlopen", fake_urlopen)
    root = download_archive("https://example.test/release.tar.gz", tmp_path / "cache")
    assert (root / "release.yml").exists()
    with pytest.raises(ValueError, match="unsupported archive source"):
        download_archive("https://example.test/release.bin", tmp_path / "cache")


def test_clone_git_source_variants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], check: bool) -> None:
        assert check is True
        calls.append(command)
        if len(calls) == 1:
            raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr("atlas.sources.subprocess.run", fake_run)
    root = clone_git_source("git+https://example.test/release.git#abc123", tmp_path / "cache")
    assert calls == [
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            "abc123",
            "https://example.test/release.git",
            str(root),
        ],
        ["git", "clone", "--depth", "1", "https://example.test/release.git", str(root)],
        ["git", "-C", str(root), "fetch", "--depth", "1", "origin", "abc123"],
        ["git", "-C", str(root), "checkout", "--detach", "FETCH_HEAD"],
    ]

    calls.clear()
    monkeypatch.setattr(
        "atlas.sources.subprocess.run",
        lambda command, check: calls.append(command),
    )
    assert resolve_source(
        "git+https://example.test/release.git",
        cache_dir=tmp_path / "cache",
    ).name.startswith("git.tmp.")
    assert calls == [
        [
            "git",
            "clone",
            "--depth",
            "1",
            "https://example.test/release.git",
            str(tmp_path / "cache/sources" / f"git.tmp.{os.getpid()}"),
        ]
    ]


def test_git_source_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="repository URL is required"):
        clone_git_source("git+", tmp_path / "cache")

    def missing(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr("atlas.sources.subprocess.run", missing)
    with pytest.raises(ValueError, match="git command is required"):
        clone_git_source("git+https://example.test/release.git", tmp_path / "cache")

    def failed(command, check):
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr("atlas.sources.subprocess.run", failed)
    with pytest.raises(ValueError, match="git source clone failed"):
        clone_git_source("git+https://example.test/release.git", tmp_path / "cache")


def test_resolve_source_validation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="release source is required"):
        resolve_source(" ")
    with pytest.raises(ValueError, match="cache_dir is required for a git"):
        resolve_source("git+https://example.test/release.git")
    with pytest.raises(ValueError, match="cache_dir is required for a remote"):
        resolve_source("https://example.test/release.tar.gz")

    archive = tmp_path / "release.zip"
    archive.write_text("not a zip", encoding="utf-8")
    with pytest.raises(ValueError, match="cache_dir is required for a release archive"):
        resolve_source(str(archive))

    unsupported = tmp_path / "release.bin"
    unsupported.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported release source"):
        resolve_source(str(unsupported))
    assert resolve_source(str(tmp_path / "missing")) == tmp_path / "missing"


def test_resolve_source_delegates_remote_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = tmp_path / "resolved"
    calls: list[tuple[str, Path]] = []
    monkeypatch.setattr(
        "atlas.sources.download_archive",
        lambda source, cache: calls.append((source, cache)) or expected,
    )
    cache = tmp_path / "cache"
    assert resolve_source("https://example.test/release.tgz", cache_dir=cache) == expected
    assert calls == [("https://example.test/release.tgz", cache)]
