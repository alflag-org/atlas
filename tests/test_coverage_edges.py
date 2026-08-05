from __future__ import annotations

import io
import os
import runpy
import shutil
import stat
import subprocess
import sys
import tarfile
import threading
import time
import warnings
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from atlas import cli
from atlas.catalog import active_releases, resolve_command
from atlas.launchers import (
    _atomic_write,
    _ensure_generation_link,
    _stage_generation,
    publish_host_artifacts,
)
from atlas.manifests import validate_name
from atlas.paths import ensure_dirs, get_paths
from atlas.releases import (
    install_release,
    read_version,
    release_digest,
    reversible_release_install,
    validate_release,
)
from atlas.runtime import RuntimeStatus, install_runtime
from atlas.sources import (
    clone_git_source,
    download_archive,
    extract_archive,
    resolve_source,
)
from atlas.yamlutil import dump_yaml_file, load_yaml_file


def _set_env(monkeypatch: pytest.MonkeyPatch, home: Path, etc: Path, var: Path) -> None:
    monkeypatch.setenv("ATLAS_HOME", str(home))
    monkeypatch.setenv("ATLAS_ETC_DIR", str(etc))
    monkeypatch.setenv("ATLAS_VAR_DIR", str(var))
    monkeypatch.setenv("ATLAS_RUNTIME_DIR", str(home / "runtime"))


def _release(
    path: Path,
    *,
    release_name: str = "default",
    command_name: str = "sample",
    version: str = "0.1.0",
) -> Path:
    (path / "modules").mkdir(parents=True, exist_ok=True)
    (path / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    module_name = command_name.replace("-", "_")
    (path / "modules" / f"{module_name}.py").write_text(
        "def main(argv: list[str] | None = None) -> int:\n    return 0\n",
        encoding="utf-8",
    )
    (path / "release.yml").write_text(
        "schema: atlas.release/v1\n"
        f"name: {release_name}\n"
        "commands:\n"
        f"  {command_name}:\n"
        f"    target: {module_name}:main\n",
        encoding="utf-8",
    )
    return path


def _fake_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[list[str]]:
    calls: list[list[str]] = []
    pyenv_root = tmp_path / "pyenv/versions/3.12.3"
    pyenv_python = pyenv_root / "bin/python"
    pyenv_python.parent.mkdir(parents=True)
    pyenv_python.write_text("", encoding="utf-8")
    monkeypatch.setattr("atlas.runtime.shutil.which", lambda _: "/usr/bin/pyenv")

    def fake_run(
        cmd: list[str],
        check: bool,
        capture_output: bool = False,
        text: bool = False,
        env: dict[str, str] | None = None,
    ):
        assert check is True
        calls.append(cmd)
        if capture_output:
            class Proc:
                stdout = f"{pyenv_root}\n"

            return Proc()
        if cmd[:3] == [str(pyenv_python), "-m", "venv"]:
            runtime_python = Path(cmd[3]) / "bin/python"
            runtime_python.parent.mkdir(parents=True, exist_ok=True)
            runtime_python.write_text("", encoding="utf-8")
        return None

    monkeypatch.setattr("atlas.runtime.subprocess.run", fake_run)
    return calls


def test_cli_status_handles_invalid_host_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True)
    (etc / "host.yml").write_text("[]\n", encoding="utf-8")
    _set_env(monkeypatch, home, etc, var)

    assert cli.main(["status"]) == 0

    assert "host name: unknown" in capsys.readouterr().out


def test_cli_status_without_host_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True)
    _set_env(monkeypatch, home, etc, var)

    assert cli.main(["status"]) == 0

    assert "host name: unknown" in capsys.readouterr().out


def test_cli_runtime_status_without_config_and_with_pyenv_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True)
    _set_env(monkeypatch, home, etc, var)
    monkeypatch.setattr(
        cli,
        "runtime_status",
        lambda runtime_root, python_version: RuntimeStatus(
            provider="pyenv",
            provider_available=True,
            artifacts_venv=home / "runtime/python/envs/scripts",
            runtime_python=home / "runtime/python/envs/scripts/bin/python",
            runtime_python_exists=False,
            pyenv_python_error="pyenv command failed",
        ),
    )

    assert cli.main(["runtime", "status"]) == 0

    out = capsys.readouterr().out
    assert "provider available: true" in out
    assert "pyenv python error: pyenv command failed" in out
    assert "runtime python exists: false" in out


def test_cli_runtime_status_without_pyenv_path_or_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True)
    _set_env(monkeypatch, home, etc, var)
    monkeypatch.setattr(
        cli,
        "runtime_status",
        lambda runtime_root, python_version: RuntimeStatus(
            provider="pyenv",
            provider_available=False,
            artifacts_venv=home / "runtime/python/envs/scripts",
            runtime_python=home / "runtime/python/envs/scripts/bin/python",
            runtime_python_exists=False,
        ),
    )

    assert cli.main(["runtime", "status"]) == 0

    out = capsys.readouterr().out
    assert "pyenv python:" not in out
    assert "pyenv python error:" not in out
    assert "artifacts venv:" in out


def test_cli_runtime_install_prints_result(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True)
    (etc / "config.yml").write_text(
        "runtime:\n  python:\n    version: '3.12.3'\nreleases: {}\n",
        encoding="utf-8",
    )
    _set_env(monkeypatch, home, etc, var)
    monkeypatch.setattr(
        cli,
        "install_runtime",
        lambda runtime, version, roots, tmp_dir=None, python_build_cache_path=None: home / "runtime/bin/python",
    )

    assert cli.main(["runtime", "install"]) == 0

    out = capsys.readouterr().out
    assert f"installed runtime python: {home / 'runtime/bin/python'}" in out
    assert "configured python version: 3.12.3" in out


def test_cli_release_update_rejects_unconfigured_release(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True)
    (etc / "config.yml").write_text(
        "runtime:\n  python:\n    version: '3.12.3'\n"
        "releases:\n  known:\n    source: sample\n",
        encoding="utf-8",
    )
    _set_env(monkeypatch, home, etc, var)

    assert cli.main(["release", "update", "missing"]) == 2
    assert "release is not configured: missing" in capsys.readouterr().err


def test_cli_release_update_with_no_enabled_releases_is_a_noop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True)
    (etc / "config.yml").write_text(
        "runtime:\n  python:\n    version: '3.12.3'\nreleases: {}\n",
        encoding="utf-8",
    )
    _set_env(monkeypatch, home, etc, var)

    assert cli.main(["release", "update"]) == 0


def test_release_shims_is_not_a_public_command() -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(["release", "shims"])
    assert error.value.code == 2


def test_release_install_rejects_broken_current_entry(tmp_path: Path) -> None:
    current = tmp_path / "current"
    current.mkdir()
    (current / "default").mkdir()
    source = _release(tmp_path / "source")

    with pytest.raises(ValueError, match="current entry must be a symlink"):
        install_release(source, tmp_path / "releases", current)

    (current / "default").rmdir()
    (tmp_path / "releases").mkdir(exist_ok=True)
    (current / "default").symlink_to(tmp_path / "releases/missing")
    with pytest.raises(ValueError, match="active release target not found"):
        install_release(source, tmp_path / "releases", current)


def test_cli_module_entrypoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["atlas", "--help"])
    monkeypatch.delitem(sys.modules, "atlas.cli", raising=False)
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("atlas.cli", run_name="__main__")
    assert exc.value.code == 0


def test_manifest_name_validation_rejects_invalid_identifiers() -> None:
    for name in ["foo--bar", "foo-", "Foo", "foo_bar", ""]:
        with pytest.raises(ValueError, match="invalid command name"):
            validate_name(name, kind="command")


def test_release_validation_rejects_missing_empty_and_invalid_inputs(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="missing VERSION file"):
        read_version(tmp_path / "missing-release")

    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "VERSION").write_text("\n", encoding="utf-8")
    with pytest.raises(ValueError, match="VERSION is empty"):
        read_version(empty)

    with pytest.raises(ValueError, match="release directory not found"):
        validate_release(tmp_path / "missing")

    for name in ["Bad", "bad_name"]:
        with pytest.raises(ValueError, match="invalid release name"):
            validate_name(name, kind="release")


def test_release_digest_rejects_missing_symlink_and_nonregular_entries(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="release directory not found"):
        release_digest(tmp_path / "missing")

    release = tmp_path / "release"
    release.mkdir()
    regular = release / "regular.txt"
    regular.write_text("content\n", encoding="utf-8")
    (release / "link").symlink_to(regular)
    with pytest.raises(ValueError, match="symlink is not allowed"):
        release_digest(release)

    (release / "link").unlink()
    os.mkfifo(release / "pipe")
    with pytest.raises(ValueError, match="regular file"):
        release_digest(release)


def test_install_release_rejects_current_link_outside_release_root(tmp_path: Path) -> None:
    source = _release(tmp_path / "source")
    releases = tmp_path / "releases"
    current = tmp_path / "current"
    current.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (current / "default").symlink_to(external, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes releases root"):
        install_release(source, releases, current)

    active = current / "default"
    active.unlink()
    active.symlink_to("../releases/default/snapshot")
    with pytest.raises(ValueError, match="path traversal"):
        install_release(source, releases, current)

    alias = releases / "alias"
    alias.symlink_to(external, target_is_directory=True)
    active.unlink()
    active.symlink_to(alias, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink chain"):
        install_release(source, releases, current)


def test_catalog_rejects_wrong_snapshot_names_symlink_chains_and_traversal(
    tmp_path: Path,
) -> None:
    source = _release(tmp_path / "source")
    releases = tmp_path / "releases"
    current = tmp_path / "current"
    target = install_release(source, releases, current)
    active = current / "default"

    external = tmp_path / "external"
    external.mkdir()
    active.unlink()
    active.symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match="outside releases root"):
        active_releases(current, releases)
    active.unlink()
    active.symlink_to("../releases/default/" + target.name)
    with pytest.raises(ValueError, match="path traversal"):
        active_releases(current, releases)

    alias = releases / "default" / "alias"
    alias.symlink_to(target, target_is_directory=True)
    active.unlink()
    active.symlink_to(alias, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink chain"):
        active_releases(current, releases)

    alias.unlink()
    active.unlink()
    wrong = releases / "default" / f"wrong-{target.name}"
    target.rename(wrong)
    active.symlink_to(wrong, target_is_directory=True)
    with pytest.raises(ValueError, match="snapshot name mismatch"):
        active_releases(current, releases)


def test_catalog_requires_a_real_releases_root_and_matching_manifest_name(
    tmp_path: Path,
) -> None:
    current = tmp_path / "current"
    current.mkdir()
    with pytest.raises(ValueError, match="releases root must be a directory"):
        active_releases(current, tmp_path / "missing-releases")

    source = _release(tmp_path / "source")
    releases = tmp_path / "releases"
    target = install_release(source, releases, current)
    for item in [*target.rglob("*"), target]:
        item.chmod(stat.S_IMODE(item.stat().st_mode) | stat.S_IWUSR)
    (target / "release.yml").write_text(
        "schema: atlas.release/v1\nname: other\ncommands:\n"
        "  sample:\n    target: sample:main\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="name mismatch"):
        active_releases(current, releases)


def test_catalog_rejects_mutated_snapshot_and_keeps_direct_snapshot_on_link_swap(
    tmp_path: Path,
) -> None:
    source = _release(tmp_path / "source")
    releases = tmp_path / "releases"
    current = tmp_path / "current"
    target = install_release(source, releases, current)
    command = resolve_command(current, releases, "sample")
    external = tmp_path / "external"
    external.mkdir()
    active = current / "default"
    active.unlink()
    active.symlink_to(external, target_is_directory=True)
    assert command.release.root == target

    active.unlink()
    active.symlink_to(target, target_is_directory=True)
    for item in [*target.rglob("*"), target]:
        item.chmod(stat.S_IMODE(item.stat().st_mode) | stat.S_IWUSR)
    (target / "modules/sample.py").write_text(
        "def main(argv: list[str] | None = None) -> int:\n    return 1\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="snapshot name mismatch"):
        active_releases(current, releases)


def test_concurrent_release_failure_cannot_rollback_a_later_activation(
    tmp_path: Path,
) -> None:
    old = _release(tmp_path / "old", version="0.1.0")
    failed = _release(tmp_path / "failed", version="0.2.0")
    winner = _release(tmp_path / "winner", version="0.3.0")
    releases = tmp_path / "releases"
    current = tmp_path / "current"
    old_target = install_release(old, releases, current)
    failed_activated = threading.Event()
    allow_failed = threading.Event()
    winner_done = threading.Event()
    errors: list[BaseException] = []

    def fail_install() -> None:
        try:
            with reversible_release_install(failed, releases, current):
                failed_activated.set()
                assert allow_failed.wait(5)
                raise RuntimeError("failed transaction")
        except BaseException as exc:
            errors.append(exc)

    def winner_install() -> None:
        try:
            install_release(winner, releases, current)
        except BaseException as exc:
            errors.append(exc)
        finally:
            winner_done.set()

    first = threading.Thread(target=fail_install)
    second = threading.Thread(target=winner_install)
    first.start()
    assert failed_activated.wait(5)
    second.start()
    time.sleep(0.1)
    assert not winner_done.is_set()
    allow_failed.set()
    first.join(5)
    second.join(5)

    assert errors and isinstance(errors[0], RuntimeError)
    assert winner_done.is_set()
    active_target = (current / "default").resolve()
    assert active_target.name.startswith("0.3.0-")
    assert old_target.is_dir()


def test_concurrent_same_digest_install_is_idempotent(tmp_path: Path) -> None:
    source = _release(tmp_path / "source")
    releases = tmp_path / "releases"
    current = tmp_path / "current"
    barrier = threading.Barrier(3)
    results: list[Path] = []
    errors: list[BaseException] = []

    def install() -> None:
        try:
            barrier.wait(5)
            results.append(install_release(source, releases, current))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=install) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait(5)
    for thread in threads:
        thread.join(5)

    assert not errors
    assert len(results) == 2
    assert results[0] == results[1] == (current / "default").resolve()
    assert list((releases / "default").glob(".*.tmp.*")) == []


def test_install_release_rejects_changed_staged_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import atlas.releases as releases_module

    source = _release(tmp_path / "source")
    original_validate = releases_module.validate_release

    def changed_staging(root: Path, **kwargs):
        validated = original_validate(root, **kwargs)
        if ".tmp." in root.name:
            return replace(validated, content_digest="0" * 64)
        return validated

    monkeypatch.setattr(releases_module, "validate_release", changed_staging)
    with pytest.raises(ValueError, match="staged release content changed"):
        install_release(source, tmp_path / "releases", tmp_path / "current")


def test_release_snapshot_rejects_existing_digest_mismatch(tmp_path: Path) -> None:
    source = _release(tmp_path / "source")
    different = _release(tmp_path / "different")
    (different / "modules/sample.py").write_text(
        "def main(argv: list[str] | None = None) -> int:\n    return 1\n",
        encoding="utf-8",
    )
    releases = tmp_path / "releases"
    current = tmp_path / "current"
    validated = validate_release(source)
    other = validate_release(different)
    target = releases / "default" / f"{validated.version}-{validated.content_digest}"
    target.parent.mkdir(parents=True)
    import shutil

    shutil.copytree(other.root, target)
    with pytest.raises(ValueError, match="snapshot digest mismatch"):
        install_release(source, releases, current)


def test_release_snapshot_revalidates_after_immutability_transition(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import atlas.releases as releases_module

    source = _release(tmp_path / "source")
    original_validate = releases_module.validate_release
    calls = 0

    def mutate_after_read_only(root: Path, **kwargs):
        nonlocal calls
        validated = original_validate(root, **kwargs)
        if ".tmp." in root.name:
            calls += 1
            if calls == 2:
                return replace(validated, content_digest="0" * 64)
        return validated

    monkeypatch.setattr(releases_module, "validate_release", mutate_after_read_only)
    with pytest.raises(ValueError, match="staged release content changed"):
        install_release(source, tmp_path / "releases", tmp_path / "current")


def test_release_target_validation_reports_empty_child_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = _release(tmp_path / "source")
    monkeypatch.setattr(
        "atlas.releases.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stderr="",
            stdout="",
        ),
    )
    with pytest.raises(ValueError, match="child exited with status 1"):
        validate_release(source)


def test_release_transient_helpers_reject_links_and_preserve_existing_target(
    tmp_path: Path,
) -> None:
    import atlas.releases as releases_module

    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("outside", encoding="utf-8")
    link = root / "link"
    link.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink is not allowed"):
        releases_module._make_tree_read_only(root)
    releases_module._make_tree_writable(root)

    source = _release(tmp_path / "source", version="0.1.0")
    old = _release(tmp_path / "old", version="0.2.0")
    releases = tmp_path / "releases"
    current = tmp_path / "current"
    install_release(source, releases, current)
    install_release(old, releases, current)
    old_target = (current / "default").resolve()
    current_target = releases / "default" / old_target.name
    assert current_target == old_target

    new = _release(tmp_path / "new", version="0.3.0")
    install_release(new, releases, current)
    releases_module._replace_symlink(current / "default", old_target)
    original_replace = releases_module._replace_symlink
    try:
        releases_module._replace_symlink = lambda link, target: (_ for _ in ()).throw(
            RuntimeError("activation failed")
        )
        with pytest.raises(RuntimeError, match="activation failed"):
            install_release(new, releases, current)
    finally:
        releases_module._replace_symlink = original_replace
    assert (current / "default").resolve() == old_target


def test_release_install_rejects_invalid_roots_and_release_directory_symlinks(
    tmp_path: Path,
) -> None:
    source = _release(tmp_path / "source")
    releases_file = tmp_path / "releases-file"
    releases_file.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ValueError, match="releases root must be a directory"):
        install_release(source, releases_file, tmp_path / "current")

    releases = tmp_path / "releases"
    releases.mkdir()
    release_link = releases / "default"
    release_link.symlink_to(tmp_path / "outside", target_is_directory=True)
    with pytest.raises(ValueError, match="release directory must not be a symlink"):
        install_release(source, releases, tmp_path / "current")


def test_release_install_cleans_staged_snapshot_after_activation_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = _release(tmp_path / "source")
    releases = tmp_path / "releases"
    current = tmp_path / "current"
    original_replace = __import__("atlas.releases").releases._replace_symlink

    def fail_activation(link: Path, target: Path) -> None:
        raise RuntimeError("activation failed")

    monkeypatch.setattr("atlas.releases._replace_symlink", fail_activation)
    with pytest.raises(RuntimeError, match="activation failed"):
        install_release(source, releases, current)
    assert list((releases / "default").glob(".*.tmp.*")) == []
    assert not list((releases / "default").glob("0.1.0-*"))
    assert original_replace is not None


def test_reversible_release_install_preserves_existing_snapshot_on_failure(
    tmp_path: Path,
) -> None:
    source = _release(tmp_path / "source")
    releases = tmp_path / "releases"
    current = tmp_path / "current"
    target = install_release(source, releases, current)

    with pytest.raises(RuntimeError, match="downstream failure"):
        with reversible_release_install(source, releases, current):
            raise RuntimeError("downstream failure")

    assert (current / "default").resolve() == target
    assert target.is_dir()

def test_install_release_rolls_back_when_replacement_rename_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = _release(tmp_path / "source", version="0.1.0")
    releases = tmp_path / "releases"
    current = tmp_path / "current"
    target = install_release(source, releases, current)
    (source / "modules/sample.py").write_text(
        "def main(argv: list[str] | None = None) -> int:\n    return 2\n",
        encoding="utf-8",
    )
    original_rename = Path.rename

    def fail_staging_rename(self: Path, target_path: Path):
        if ".tmp." in self.name:
            raise RuntimeError("rename failed")
        return original_rename(self, target_path)

    monkeypatch.setattr(Path, "rename", fail_staging_rename)
    with pytest.raises(RuntimeError, match="rename failed"):
        install_release(source, releases, current)

    assert (current / "default").resolve() == target
    assert target.exists()


def test_install_release_cleans_staging_when_initial_rename_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = _release(tmp_path / "source", version="0.1.0")
    releases = tmp_path / "releases"
    current = tmp_path / "current"
    original_rename = Path.rename

    def fail_staging_rename(self: Path, target_path: Path):
        if ".tmp." in self.name:
            raise RuntimeError("rename failed")
        return original_rename(self, target_path)

    monkeypatch.setattr(Path, "rename", fail_staging_rename)
    with pytest.raises(RuntimeError, match="rename failed"):
        install_release(source, releases, current)

    assert list((releases / "default").glob(".*.tmp.*")) == []
    assert not list((releases / "default").iterdir())


def test_install_release_rejects_non_directory_release_target(tmp_path: Path) -> None:
    source = _release(tmp_path / "source")
    release = validate_release(source)
    target = tmp_path / "releases/default" / (
        f"{release.version}-{release.content_digest}"
    )
    target.parent.mkdir(parents=True)
    target.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ValueError, match="release snapshot must be a directory"):
        install_release(source, tmp_path / "releases", tmp_path / "current")


def test_install_release_rejects_tampered_existing_snapshot(tmp_path: Path) -> None:
    source = _release(tmp_path / "source")
    releases = tmp_path / "releases"
    current = tmp_path / "current"
    target = install_release(source, releases, current)
    for item in [*target.rglob("*"), target]:
        item.chmod(stat.S_IMODE(item.stat().st_mode) | 0o200)
    (target / "tampered.txt").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not a validated release snapshot"):
        install_release(source, releases, current)
    assert (current / "default").resolve() == target


def test_reversible_release_install_restores_same_version_contents(tmp_path: Path) -> None:
    source = _release(tmp_path / "source")
    releases = tmp_path / "releases"
    current = tmp_path / "current"
    old_target = install_release(source, releases, current)
    (source / "modules/sample.py").write_text(
        "def main(argv: list[str] | None = None) -> int:\n    return 2\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="downstream failure"):
        with reversible_release_install(source, releases, current):
            raise RuntimeError("downstream failure")

    assert (old_target / "modules/sample.py").read_text(encoding="utf-8").endswith(
        "return 0\n"
    )
    assert (current / "default").resolve() == old_target
    assert len(list((releases / "default").glob("0.1.0-*"))) == 1


def test_reversible_release_install_reports_failed_link_restore(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    old = _release(tmp_path / "old", version="0.1.0")
    new = _release(tmp_path / "new", version="0.2.0")
    releases = tmp_path / "releases"
    current = tmp_path / "current"
    old_target = install_release(old, releases, current)
    original_replace = __import__("atlas.releases").releases._replace_symlink
    calls = 0

    def fail_restore(link: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("link restore failed")
        original_replace(link, target)

    monkeypatch.setattr("atlas.releases._replace_symlink", fail_restore)
    with pytest.raises(RuntimeError, match="release installation failed and rollback failed"):
        with reversible_release_install(new, releases, current):
            raise RuntimeError("downstream failure")

    assert old_target.is_dir()
    assert not (current / "default").exists()


def test_install_release_rejects_non_directory_current_root(tmp_path: Path) -> None:
    source = _release(tmp_path / "source")
    current = tmp_path / "current"
    current.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ValueError, match="current root must be a directory"):
        install_release(source, tmp_path / "releases", current)

    assert current.read_text(encoding="utf-8") == "not a directory"


def test_runner_resolve_unknown_command(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown command: missing"):
        resolve_command(tmp_path / "current", tmp_path / "releases", "missing")


def test_runner_redacts_sensitive_arguments(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True)
    var.mkdir(parents=True)
    _set_env(monkeypatch, home, etc, var)
    runtime_python = home / "runtime/python/envs/scripts/bin/python"
    runtime_python.parent.mkdir(parents=True)
    runtime_python.symlink_to(Path("/usr/bin/python3"))
    release = _release(tmp_path / "release")
    install_release(release, home / "releases", home / "current")
    other = _release(tmp_path / "other", release_name="other", command_name="other")
    install_release(other, home / "releases", home / "current")
    paths = get_paths()
    ensure_dirs(paths)
    publish_host_artifacts(paths)
    assert cli.main(["run", "sample", "--token", "abc", "--api-key=def", "DB_PASSWORD=ghi"]) == 0

    log = (var / "logs/runs.jsonl").read_text(encoding="utf-8")
    assert '"args": ["--token", "***", "--api-key=***", "DB_PASSWORD=***"]' in log


def test_runtime_install_uses_no_extra_requirements_for_release_without_requirements(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _fake_runtime(monkeypatch, tmp_path)
    release_root = tmp_path / "current/default"
    release_root.mkdir(parents=True)

    install_runtime(tmp_path / "runtime", "3.12.3", [release_root])

    assert calls[-2][1:] == ["-m", "pip", "install", "PyYAML"]


def test_runtime_helpers_report_subprocess_failures(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def missing_run(*args, **kwargs):
        raise FileNotFoundError("missing")

    monkeypatch.setattr("atlas.runtime.subprocess.run", missing_run)
    monkeypatch.setattr("atlas.runtime.shutil.which", lambda _: "/usr/bin/pyenv")
    with pytest.raises(ValueError, match="pyenv command is required"):
        install_runtime(tmp_path / "runtime", "3.12.3")

    def failing_run(cmd, check, capture_output=False, text=False, env=None):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr("atlas.runtime.subprocess.run", failing_run)
    with pytest.raises(ValueError, match="pyenv command failed"):
        install_runtime(tmp_path / "runtime", "3.12.3")


def test_runtime_status_reports_pyenv_prefix_failures(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("atlas.runtime.shutil.which", lambda _: "/usr/bin/pyenv")

    def missing_pyenv(cmd, check, capture_output=False, text=False, env=None):
        raise FileNotFoundError("pyenv")

    monkeypatch.setattr("atlas.runtime.subprocess.run", missing_pyenv)
    status = __import__("atlas.runtime").runtime.runtime_status(tmp_path / "runtime", "3.12.3")
    assert status.pyenv_python_error == "pyenv command is required for atlas runtime install"

    def failed_pyenv(cmd, check, capture_output=False, text=False, env=None):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr("atlas.runtime.subprocess.run", failed_pyenv)
    status = __import__("atlas.runtime").runtime.runtime_status(tmp_path / "runtime", "3.12.3")
    assert status.pyenv_python_error == "pyenv command failed: pyenv prefix 3.12.3"


def test_runtime_install_rejects_empty_pyenv_prefix_and_missing_python(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("atlas.runtime.shutil.which", lambda _: "/usr/bin/pyenv")

    def empty_prefix(cmd, check, capture_output=False, text=False, env=None):
        class Proc:
            stdout = "\n"

        return Proc()

    monkeypatch.setattr("atlas.runtime.subprocess.run", empty_prefix)
    with pytest.raises(ValueError, match="did not return an install prefix"):
        install_runtime(tmp_path / "runtime", "3.12.3")

    def missing_python(cmd, check, capture_output=False, text=False, env=None):
        class Proc:
            stdout = f"{tmp_path / 'pyenv/versions/3.12.3'}\n"

        return Proc()

    monkeypatch.setattr("atlas.runtime.subprocess.run", missing_python)
    with pytest.raises(ValueError, match="pyenv Python executable not found"):
        install_runtime(tmp_path / "runtime", "3.12.3")


def test_runtime_install_rolls_back_existing_venv_when_final_rename_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_runtime(monkeypatch, tmp_path)
    runtime = tmp_path / "runtime"
    scripts = runtime / "python/envs/scripts"
    scripts.mkdir(parents=True)
    (scripts / "old.txt").write_text("old", encoding="utf-8")
    original_rename = Path.rename

    def fail_tmp_scripts_rename(self: Path, target_path: Path):
        if self.name.startswith("scripts.tmp."):
            raise RuntimeError("rename failed")
        return original_rename(self, target_path)

    monkeypatch.setattr(Path, "rename", fail_tmp_scripts_rename)
    with pytest.raises(RuntimeError, match="rename failed"):
        install_runtime(runtime, "3.12.3")

    assert (scripts / "old.txt").exists()


def test_runtime_install_leaves_no_venv_when_final_rename_fails_without_backup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_runtime(monkeypatch, tmp_path)
    runtime = tmp_path / "runtime"
    original_rename = Path.rename

    def fail_tmp_scripts_rename(self: Path, target_path: Path):
        if self.name.startswith("scripts.tmp."):
            raise RuntimeError("rename failed")
        return original_rename(self, target_path)

    monkeypatch.setattr(Path, "rename", fail_tmp_scripts_rename)
    with pytest.raises(RuntimeError, match="rename failed"):
        install_runtime(runtime, "3.12.3")

    assert not (runtime / "python/envs/scripts").exists()


def test_catalog_rejects_invalid_current_root_and_entries(tmp_path: Path) -> None:
    releases = tmp_path / "releases"
    releases.mkdir()
    assert active_releases(tmp_path / "missing", releases) == []

    current = tmp_path / "current"
    current.write_text("not a dir", encoding="utf-8")
    with pytest.raises(ValueError, match="current root must be a directory"):
        active_releases(current, releases)

    current.unlink()
    current.mkdir()
    (current / "regular").write_text("ignored", encoding="utf-8")
    with pytest.raises(ValueError, match="current entry must be a symlink"):
        active_releases(current, releases)

    (current / "regular").unlink()
    (current / "Bad").symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="invalid release name"):
        active_releases(current, releases)

    (current / "Bad").unlink()
    (current / "missing").symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="active release target not found"):
        active_releases(current, releases)


def test_publish_host_artifacts_rejects_command_path_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    _set_env(monkeypatch, home, etc, var)
    paths = get_paths()
    ensure_dirs(paths)
    release = _release(tmp_path / "release")
    install_release(release, paths.releases_root, paths.current_root)
    shims = paths.shims
    (shims / "sample").mkdir(parents=True)

    with pytest.raises(ValueError, match="duplicate generated shim"):
        publish_host_artifacts(paths)


def test_host_artifact_publication_rejects_unsafe_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    _set_env(monkeypatch, home, etc, var)
    paths = get_paths()
    ensure_dirs(paths)

    regular = tmp_path / "regular"
    regular.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="active artifact generation must be a symlink"):
        paths.artifact_current.write_text("x", encoding="utf-8")
        publish_host_artifacts(paths)
    paths.artifact_current.unlink()

    external = tmp_path / "external"
    external.mkdir()
    paths.artifact_current.symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes its root"):
        publish_host_artifacts(paths)
    paths.artifact_current.unlink()
    paths.artifact_current.symlink_to(
        paths.artifact_root / "generations/missing",
        target_is_directory=True,
    )
    with pytest.raises(ValueError, match="active artifact generation is missing"):
        publish_host_artifacts(paths)
    paths.artifact_current.unlink()

    artifact_file = tmp_path / "artifact-file"
    artifact_file.mkdir()
    with pytest.raises(ValueError, match="host artifact must be a regular file"):
        _atomic_write(artifact_file, "new")
    artifact_target = tmp_path / "artifact-target"
    artifact_target.write_text("x", encoding="utf-8")
    artifact_link = tmp_path / "artifact-link"
    artifact_link.symlink_to(artifact_target)
    with pytest.raises(ValueError, match="host artifact must be a regular file"):
        _atomic_write(artifact_link, "new")

    outside_link = tmp_path / "outside-link"
    outside_link.symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match="link escapes Atlas home"):
        _ensure_generation_link(outside_link, Path("inside"))
    external_python = home / "lib/python"
    external_python.parent.mkdir(parents=True, exist_ok=True)
    external_python.symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match="link escapes Atlas home"):
        publish_host_artifacts(paths)
    external_python.unlink()
    non_directory = tmp_path / "non-directory"
    non_directory.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="destination must be a directory"):
        _ensure_generation_link(non_directory, Path("inside"))

    legacy = tmp_path / "legacy"
    legacy.mkdir()
    monkeypatch.setattr(
        Path,
        "symlink_to",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("link failed")),
    )
    with pytest.raises(OSError, match="link failed"):
        _ensure_generation_link(legacy, Path("inside"))
    assert legacy.is_dir()
    missing_link = tmp_path / "missing-link"
    with pytest.raises(OSError, match="link failed"):
        _ensure_generation_link(missing_link, Path("inside"))


def test_host_artifact_publication_rolls_back_stable_links(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    _set_env(monkeypatch, home, etc, var)
    paths = get_paths()
    ensure_dirs(paths)
    legacy_python = home / "lib/python"
    legacy_python.mkdir(parents=True)
    legacy_shims = paths.shims
    legacy_shims.mkdir()
    monkeypatch.setattr(
        "atlas.launchers._publish_current",
        lambda paths, generation: (_ for _ in ()).throw(OSError("publish failed")),
    )
    with pytest.raises(OSError, match="publish failed"):
        publish_host_artifacts(paths)
    assert legacy_python.is_dir()
    assert legacy_shims.is_dir()
    assert not paths.artifact_current.exists()


def test_host_artifact_publication_rejects_bad_generation_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    _set_env(monkeypatch, home, etc, var)
    paths = get_paths()
    generations = paths.artifact_root / "generations"
    paths.artifact_root.mkdir(parents=True)
    generations.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ValueError, match="artifact generations path must be a directory"):
        ensure_dirs(paths)
    generations.unlink()
    ensure_dirs(paths)
    generations.rmdir()
    generations.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ValueError, match="artifact generations path must be a directory"):
        publish_host_artifacts(paths)


def test_host_artifact_staging_rejects_non_regular_runner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    _set_env(monkeypatch, home, etc, var)
    paths = get_paths()
    ensure_dirs(paths)
    original_copyfile = shutil.copyfile

    def symlink_target(source: Path, destination: Path, *args, **kwargs) -> Path:
        result = original_copyfile(source, destination)
        if Path(destination).name == "target_contract.py":
            Path(destination).unlink()
            Path(destination).symlink_to(source)
        return result

    monkeypatch.setattr("atlas.launchers.shutil.copyfile", symlink_target)
    with pytest.raises(ValueError, match="staged host artifact is not a regular file"):
        _stage_generation(paths, [])


def test_sources_rejects_absolute_archive_members(tmp_path: Path) -> None:
    tar_path = tmp_path / "absolute.tar"
    with tarfile.open(tar_path, "w") as tf:
        info = tarfile.TarInfo("/absolute.py")
        info.size = 0
        tf.addfile(info, io.BytesIO())
    with pytest.raises(ValueError, match="absolute path is not allowed"):
        resolve_source(str(tar_path), cache_dir=tmp_path / "cache")

    zip_path = tmp_path / "absolute.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("/absolute.py", "")
    with pytest.raises(ValueError, match="absolute path is not allowed"):
        resolve_source(str(zip_path), cache_dir=tmp_path / "cache")


def test_sources_rejects_zip_symlink(tmp_path: Path) -> None:
    zip_path = tmp_path / "link.zip"
    info = zipfile.ZipInfo("release/link.py")
    info.external_attr = 0o120777 << 16
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(info, "target")

    with pytest.raises(ValueError, match="archive link is not allowed"):
        resolve_source(str(zip_path), cache_dir=tmp_path / "cache")


def test_sources_accepts_archive_with_release_at_root(tmp_path: Path) -> None:
    release = _release(tmp_path / "release")
    archive = tmp_path / "root.tar"
    with tarfile.open(archive, "w") as tf:
        for item in release.rglob("*"):
            tf.add(item, arcname=str(item.relative_to(release)))

    resolved = resolve_source(str(archive), cache_dir=tmp_path / "cache")

    assert resolved.name.startswith("archive.tmp.")
    assert (resolved / "VERSION").exists()


def test_sources_rejects_archives_without_release_and_unsupported_archive(tmp_path: Path) -> None:
    archive = tmp_path / "bad.tar"
    with tarfile.open(archive, "w") as tf:
        info = tarfile.TarInfo("README.md")
        info.size = 0
        tf.addfile(info, io.BytesIO())
    with pytest.raises(ValueError, match="does not contain an Atlas release"):
        resolve_source(str(archive), cache_dir=tmp_path / "cache")

    unsupported = tmp_path / "release.rar"
    unsupported.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported release source"):
        resolve_source(str(unsupported), cache_dir=tmp_path / "cache")

    with pytest.raises(ValueError, match="unsupported archive source"):
        extract_archive(unsupported, tmp_path / "cache")


def test_sources_tar_extract_falls_back_without_data_filter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    release = _release(tmp_path / "release")
    archive = tmp_path / "release.tar"
    with tarfile.open(archive, "w") as tf:
        tf.add(release, arcname="release")
    monkeypatch.setattr("atlas.sources.tarfile", SimpleNamespace(open=tarfile.open))

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        resolved = extract_archive(archive, tmp_path / "cache")

    assert (resolved / "VERSION").exists()


def test_sources_rejects_remote_archive_without_archive_suffix(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported archive source"):
        download_archive("https://example.test/release.bin", tmp_path / "cache")


def test_sources_git_variants_and_failures(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr("atlas.sources.subprocess.run", lambda cmd, check: calls.append(cmd))
    resolved = resolve_source("git+https://example.test/releases.git", cache_dir=tmp_path / "cache")
    assert resolved == tmp_path / "cache/sources" / f"git.tmp.{__import__('os').getpid()}"
    assert calls == [
        ["git", "clone", "--depth", "1", "https://example.test/releases.git", str(resolved)]
    ]

    with pytest.raises(ValueError, match="repository URL is required"):
        clone_git_source("git+", tmp_path / "cache")

    def missing_git(cmd, check):
        raise FileNotFoundError("git")

    monkeypatch.setattr("atlas.sources.subprocess.run", missing_git)
    with pytest.raises(ValueError, match="git command is required"):
        clone_git_source("git+https://example.test/releases.git", tmp_path / "cache")

    def failed_git(cmd, check):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr("atlas.sources.subprocess.run", failed_git)
    with pytest.raises(ValueError, match="git source clone failed"):
        clone_git_source("git+https://example.test/releases.git", tmp_path / "cache")


def test_resolve_source_requires_cache_for_remote_sources_and_handles_missing_local(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="release source is required"):
        resolve_source("  ")
    with pytest.raises(ValueError, match="cache_dir is required for a git"):
        resolve_source("git+https://example.test/releases.git")
    with pytest.raises(ValueError, match="cache_dir is required for a remote release archive"):
        resolve_source("https://example.test/release.tar.gz")

    archive = tmp_path / "release.zip"
    archive.write_text("not zip", encoding="utf-8")
    with pytest.raises(ValueError, match="cache_dir is required for a release archive"):
        resolve_source(str(archive))

    assert resolve_source(str(tmp_path / "missing")) == tmp_path / "missing"
    assert resolve_source("alias") == Path("alias")
    assert resolve_source("missing", cache_dir=tmp_path / "cache") == Path("missing")


def test_yaml_utilities(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="file not found"):
        load_yaml_file(tmp_path / "missing.yml")

    path = tmp_path / "nested/config.yml"
    dump_yaml_file(path, {"a": 1})
    assert load_yaml_file(path) == {"a": 1}

    path.write_text("a: 1\na: 2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate YAML key: a"):
        load_yaml_file(path)
