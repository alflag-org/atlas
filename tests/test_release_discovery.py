from __future__ import annotations

import io
import os
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from atlas.catalog import active_releases, command_index
from atlas.manifests import load_manifest
from atlas.releases import install_release
from atlas.sources import resolve_source


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("print('x')\n", encoding="utf-8")


def test_install_rejects_release_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source"
    commands = source / "commands"
    modules = source / "modules"
    commands.mkdir(parents=True)
    modules.mkdir(parents=True)
    (source / "VERSION").write_text("2026.05.10-001\n", encoding="utf-8")
    _touch(commands / "sample.py")
    (source / "release.yml").write_text(
        "schema: atlas.release/v1\n"
        "name: default\n"
        "commands:\n"
        "  sample:\n"
        "    runtime: python\n"
        "    entrypoint: commands/sample.py\n",
        encoding="utf-8",
    )
    target = source / "target.txt"
    target.write_text("x", encoding="utf-8")
    (modules / "bad-link.py").symlink_to(target)

    with pytest.raises(ValueError, match="symlink is not allowed"):
        install_release(source, tmp_path / "releases", tmp_path / "current")


def test_install_overwrites_same_version_atomically(tmp_path: Path) -> None:
    source = tmp_path / "source"
    commands = source / "commands"
    commands.mkdir(parents=True)
    (source / "VERSION").write_text("2026.05.10-001\n", encoding="utf-8")
    (commands / "sample.py").write_text("print('v1')\n", encoding="utf-8")
    (source / "release.yml").write_text(
        "schema: atlas.release/v1\n"
        "name: default\n"
        "commands:\n"
        "  sample:\n"
        "    runtime: python\n"
        "    entrypoint: commands/sample.py\n",
        encoding="utf-8",
    )

    releases = tmp_path / "releases"
    current = tmp_path / "current"
    install_release(source, releases, current)
    target = releases / "default/2026.05.10-001"
    assert target.exists()
    assert (current / "default").resolve() == target
    assert (target / "commands/sample.py").read_text(encoding="utf-8") == "print('v1')\n"

    (commands / "sample.py").write_text("print('v2')\n", encoding="utf-8")
    install_release(source, releases, current)
    assert (current / "default").resolve() == target
    assert (target / "commands/sample.py").read_text(encoding="utf-8") == "print('v2')\n"


def test_install_cleans_staging_and_backup_paths(tmp_path: Path) -> None:
    source = _release(tmp_path / "source")
    releases = tmp_path / "releases"
    current = tmp_path / "current"

    install_release(source, releases, current)
    install_release(source, releases, current)

    assert list((releases / "default").glob("*.tmp.*")) == []
    assert list((releases / "default").glob("*.bak.*")) == []


def _release(
    path: Path,
    *,
    release_name: str = "default",
    command_name: str = "sample",
    version: str = "2026.05.10-001",
) -> Path:
    commands = path / "commands"
    modules = path / "modules"
    commands.mkdir(parents=True)
    modules.mkdir(parents=True)
    (path / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    _touch(commands / f"{command_name}.py")
    (path / "release.yml").write_text(
        "schema: atlas.release/v1\n"
        f"name: {release_name}\n"
        "commands:\n"
        f"  {command_name}:\n"
        "    runtime: python\n"
        f"    entrypoint: commands/{command_name}.py\n",
        encoding="utf-8",
    )
    return path


def test_install_release_supports_multiple_active_releases(tmp_path: Path) -> None:
    common = _release(
        tmp_path / "common",
        release_name="common",
        command_name="common-command",
        version="0.1.0",
    )
    kitsunebi = _release(
        tmp_path / "kitsunebi",
        release_name="kitsunebi",
        command_name="kitsunebi-command",
        version="0.2.0",
    )
    releases = tmp_path / "releases"
    current = tmp_path / "current"

    common_target = install_release(common, releases, current)
    kitsunebi_target = install_release(kitsunebi, releases, current)

    assert common_target == releases / "common/0.1.0"
    assert kitsunebi_target == releases / "kitsunebi/0.2.0"
    assert (current / "common").resolve() == common_target
    assert (current / "kitsunebi").resolve() == kitsunebi_target
    assert [release.name for release in active_releases(current)] == ["common", "kitsunebi"]
    assert sorted(command_index(current)) == ["common-command", "kitsunebi-command"]


def test_install_release_rejects_current_root_symlink(tmp_path: Path) -> None:
    source = _release(tmp_path / "source")
    releases = tmp_path / "releases"
    current = tmp_path / "current"
    redirected_target = tmp_path / "redirected-target"
    redirected_target.mkdir(parents=True)
    current.symlink_to(redirected_target, target_is_directory=True)

    with pytest.raises(ValueError, match="current root must be a directory"):
        install_release(source, releases, current)

    assert current.is_symlink()
    assert current.resolve() == redirected_target


def test_resolve_local_tar_archive(tmp_path: Path) -> None:
    source = _release(tmp_path / "release")
    archive = tmp_path / "release.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(source, arcname="release")

    resolved = resolve_source(str(archive), cache_dir=tmp_path / "cache")

    assert (resolved / "VERSION").read_text(encoding="utf-8").strip() == "2026.05.10-001"
    assert list(load_manifest(resolved).commands) == ["sample"]


def test_resolve_archive_replaces_stale_cache_tmp(tmp_path: Path) -> None:
    source = _release(tmp_path / "release")
    archive = tmp_path / "release.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(source, arcname="release")
    stale_tmp = tmp_path / "cache" / "sources" / f"archive.tmp.{os.getpid()}"
    stale_tmp.mkdir(parents=True)
    (stale_tmp / "stale.txt").write_text("stale", encoding="utf-8")

    resolve_source(str(archive), cache_dir=tmp_path / "cache")

    assert not (stale_tmp / "stale.txt").exists()


def test_resolve_local_zip_archive(tmp_path: Path) -> None:
    source = _release(tmp_path / "release")
    archive = tmp_path / "release.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for item in source.rglob("*"):
            zf.write(item, item.relative_to(source.parent))

    resolved = resolve_source(str(archive), cache_dir=tmp_path / "cache")

    assert (resolved / "VERSION").exists()
    assert (resolved / "commands/sample.py").exists()


def test_resolve_archive_rejects_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "bad.tar"
    payload = b"bad"
    with tarfile.open(archive, "w") as tf:
        info = tarfile.TarInfo("../bad.py")
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))

    with pytest.raises(ValueError, match="path traversal"):
        resolve_source(str(archive), cache_dir=tmp_path / "cache")


def test_resolve_archive_rejects_links(tmp_path: Path) -> None:
    archive = tmp_path / "bad.tar"
    with tarfile.open(archive, "w") as tf:
        info = tarfile.TarInfo("release/modules/link.py")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tf.addfile(info)

    with pytest.raises(ValueError, match="link is not allowed"):
        resolve_source(str(archive), cache_dir=tmp_path / "cache")


def test_resolve_http_archive(monkeypatch, tmp_path: Path) -> None:
    source = _release(tmp_path / "release")
    archive = tmp_path / "release.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(source, arcname="release")
    data = archive.read_bytes()

    def fake_urlopen(_source: str, timeout: int):
        assert timeout == 30
        return io.BytesIO(data)

    monkeypatch.setattr("atlas.sources.urlopen", fake_urlopen)

    resolved = resolve_source("https://example.test/release.tar.gz", cache_dir=tmp_path / "cache")

    assert (resolved / "VERSION").exists()


def test_resolve_git_source_with_ref(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], check: bool) -> None:
        assert check is True
        calls.append(cmd)

    monkeypatch.setattr("atlas.sources.subprocess.run", fake_run)

    resolved = resolve_source(
        "git+https://example.test/releases.git#v1.0.0",
        cache_dir=tmp_path / "cache",
    )

    assert resolved == tmp_path / "cache" / "sources" / f"git.tmp.{os.getpid()}"
    assert calls == [
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            "v1.0.0",
            "https://example.test/releases.git",
            str(resolved),
        ]
    ]


def test_resolve_git_source_ref_falls_back_to_fetch(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], check: bool) -> None:
        assert check is True
        calls.append(cmd)
        if len(calls) == 1:
            raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr("atlas.sources.subprocess.run", fake_run)

    resolved = resolve_source(
        "git+https://example.test/releases.git#abc123",
        cache_dir=tmp_path / "cache",
    )

    assert calls == [
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            "abc123",
            "https://example.test/releases.git",
            str(resolved),
        ],
        ["git", "clone", "--depth", "1", "https://example.test/releases.git", str(resolved)],
        ["git", "-C", str(resolved), "fetch", "--depth", "1", "origin", "abc123"],
        ["git", "-C", str(resolved), "checkout", "--detach", "FETCH_HEAD"],
    ]
