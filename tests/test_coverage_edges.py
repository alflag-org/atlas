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
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import atlas.generations as generations_module
import atlas.jobs as jobs_module
import atlas.runtime as runtime_module
import atlas_core.generations as artifact_generations_module
from atlas import cli
from atlas.catalog import active_releases, release_from_snapshot, resolve_command
from atlas.execution import (
    _await_generation_lease_ack,
    _capture_generation_snapshot,
    _selected_executable,
    pinned_execution_selection,
    resolve_command_for_execution,
)
from atlas.generations import (
    _generation_lease,
    _generation_lease_handoff,
    _leased_names,
    _remove_unleased_generation,
    active_generation,
    collect_generation_garbage,
    generation_lease,
)
from atlas.launchers import (
    _atomic_write,
    _capture_launcher,
    _copy_state_entry,
    _ensure_generation_link,
    _restore_launcher,
    _stage_generation,
    publish_host_artifacts,
)
from atlas.manifests import validate_name
from atlas.paths import ensure_dirs, get_paths
from atlas.releases import (
    _current_target,
    _validate_targets_in_child,
    install_release,
    read_version,
    release_digest,
    reversible_release_install,
    reversible_release_transaction,
    validate_release,
    validate_release_targets,
)
from atlas.runtime import (
    RuntimeCandidate,
    RuntimeStatus,
    _install_atlas_core,
    _runtime_generations,
    _runtime_link_target,
    _runtime_requirements,
    _site_packages,
    install_runtime,
)
from atlas.sources import (
    clone_git_source,
    download_archive,
    extract_archive,
    resolve_source,
)
from atlas.yamlutil import dump_yaml_file, load_yaml_file
from atlas_core.generations import (
    _generation_lease as artifact_generation_lease,
)
from atlas_core.generations import (
    generation_lease_from_environment,
)


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


def _wheel(path: Path, package: str, version: str, value: str) -> Path:
    distribution = f"{package}-{version}"
    dist_info = f"{distribution}.dist-info"
    wheel = path / f"{distribution}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(f"{package}/__init__.py", f"VALUE = {value!r}\n")
        archive.writestr(
            f"{dist_info}/METADATA",
            f"Metadata-Version: 2.1\nName: {package}\nVersion: {version}\n",
        )
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: atlas-test\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(
            f"{dist_info}/RECORD",
            f"{package}/__init__.py,,\n"
            f"{dist_info}/METADATA,,\n"
            f"{dist_info}/WHEEL,,\n"
            f"{dist_info}/RECORD,,\n",
        )
    return wheel


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


def _fast_release_runtime(monkeypatch: pytest.MonkeyPatch, runtime_root: Path) -> None:
    """Keep lock-order tests independent of pip and venv build time."""

    @contextmanager
    def prepared(*args, **kwargs):
        generations = runtime_root / "python/envs/generations"
        root = generations / f"scripts.test.{threading.get_ident()}"
        (root / "bin").mkdir(parents=True, exist_ok=True)
        python = root / "bin/python"
        if not python.exists():
            python.symlink_to(Path(sys.executable))
        yield RuntimeCandidate(root=root, python=Path(sys.executable))

    monkeypatch.setattr("atlas.releases.prepared_runtime", prepared)


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
        lambda runtime, version, roots, tmp_dir=None, python_build_cache_path=None, validate_candidate=None: home
        / "runtime/bin/python",
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

    invalid_version = tmp_path / "invalid-version"
    invalid_version.mkdir()
    (invalid_version / "VERSION").write_text("1/2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid release version"):
        read_version(invalid_version)

    symlinked = _release(tmp_path / "symlinked")
    (symlinked / "external").mkdir()
    (symlinked / "modules/linked").symlink_to(symlinked / "external")
    with pytest.raises(ValueError, match="symlink is not allowed"):
        validate_release(symlinked, validate_targets=False)

    with pytest.raises(ValueError, match="release directory not found"):
        validate_release(tmp_path / "missing")

    for name in ["Bad", "bad_name"]:
        with pytest.raises(ValueError, match="invalid release name"):
            validate_name(name, kind="release")


def test_validate_release_requires_a_child_runtime(tmp_path: Path) -> None:
    source = _release(tmp_path / "source")
    with pytest.raises(ValueError, match="target validation runtime is required"):
        validate_release(source)
    validated = validate_release(source, runtime_python=Path(sys.executable))
    assert validated.manifest.name == "default"


def test_current_target_validation_rejects_each_malformed_link(tmp_path: Path) -> None:
    releases = tmp_path / "releases"
    releases.mkdir()
    current = tmp_path / "current"
    current.mkdir()
    link = current / "default"

    assert _current_target(link, releases) is None
    link.write_text("not a link", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a symlink"):
        _current_target(link, releases)

    link.unlink()
    link.symlink_to("../outside")
    with pytest.raises(ValueError, match="path traversal"):
        _current_target(link, releases)

    link.unlink()
    external = tmp_path / "external"
    external.mkdir()
    link.symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes releases root"):
        _current_target(link, releases)

    link.unlink()
    (releases / "default").mkdir()
    (releases / "default" / "alias").symlink_to(external, target_is_directory=True)
    link.symlink_to(releases / "default" / "alias", target_is_directory=True)
    with pytest.raises(ValueError, match="symlink chain"):
        _current_target(link, releases)

    link.unlink()
    (releases / "default" / "alias").unlink()
    link.symlink_to(releases / "default" / "missing", target_is_directory=True)
    with pytest.raises(ValueError, match="target not found"):
        _current_target(link, releases)

    link.unlink()
    source = _release(tmp_path / "source")
    snapshot = releases / "default" / "wrong-name"
    shutil.copytree(source, snapshot)
    link.symlink_to(snapshot, target_is_directory=True)
    with pytest.raises(ValueError, match="not a validated release snapshot"):
        _current_target(link, releases)


def test_release_transaction_rejects_duplicate_names(tmp_path: Path) -> None:
    source = _release(tmp_path / "source")
    with pytest.raises(ValueError, match="duplicate release names"):
        with reversible_release_transaction(
            [source, source],
            tmp_path / "releases",
            tmp_path / "current",
            runtime_root=tmp_path / "runtime",
            python_version="3.14.6",
        ):
            pass


def test_release_transaction_cleanup_handles_no_created_snapshot(
    tmp_path: Path,
) -> None:
    source = _release(tmp_path / "source")
    releases = tmp_path / "releases"
    (releases / "default").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    release_dir = releases / "default"
    release_dir.rmdir()
    release_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="release directory must not be a symlink"):
        install_release(source, releases, tmp_path / "current")


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

    with pytest.raises(ValueError, match="outside releases root"):
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
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    old = _release(tmp_path / "old", version="0.1.0")
    failed = _release(tmp_path / "failed", version="0.2.0")
    winner = _release(tmp_path / "winner", version="0.3.0")
    releases = tmp_path / "releases"
    current = tmp_path / "current"
    _fast_release_runtime(monkeypatch, tmp_path / "runtime")
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


def test_concurrent_same_digest_install_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = _release(tmp_path / "source")
    releases = tmp_path / "releases"
    current = tmp_path / "current"
    _fast_release_runtime(monkeypatch, tmp_path / "runtime")
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


def test_release_validation_uses_candidate_requirements_without_runtime(
    tmp_path: Path,
) -> None:
    dependency = _wheel(tmp_path, "candidate_dependency", "1.0.0", "one")
    source = _release(tmp_path / "source")
    (source / "requirements.txt").write_text(f"{dependency}\n", encoding="utf-8")
    (source / "modules/sample.py").write_text(
        "from candidate_dependency import VALUE\n"
        "def main(argv: list[str] | None = None) -> int:\n"
        "    return 0 if VALUE == 'one' else 1\n",
        encoding="utf-8",
    )

    target = install_release(source, tmp_path / "releases", tmp_path / "current")

    assert (tmp_path / "current/default").resolve() == target


def test_release_validation_inherits_atlas_core_dependencies_without_runtime(
    tmp_path: Path,
) -> None:
    source = _release(tmp_path / "source")
    (source / "modules/sample.py").write_text(
        "import yaml\n"
        "def main(argv: list[str] | None = None) -> int:\n"
        "    return 0 if yaml.__name__ == 'yaml' else 1\n",
        encoding="utf-8",
    )

    target = install_release(source, tmp_path / "releases", tmp_path / "current")

    assert (tmp_path / "current/default").resolve() == target


def test_release_update_validates_a_new_candidate_dependency_without_runtime(
    tmp_path: Path,
) -> None:
    first_dependency = _wheel(tmp_path, "candidate_dependency", "1.0.0", "one")
    second_dependency = _wheel(tmp_path, "candidate_dependency", "2.0.0", "two")
    source = _release(tmp_path / "source")
    requirements = source / "requirements.txt"
    module = source / "modules/sample.py"
    requirements.write_text(f"{first_dependency}\n", encoding="utf-8")
    module.write_text(
        "from candidate_dependency import VALUE\n"
        "def main(argv: list[str] | None = None) -> int:\n"
        "    return 0 if VALUE == 'one' else 1\n",
        encoding="utf-8",
    )
    releases = tmp_path / "releases"
    current = tmp_path / "current"
    install_release(source, releases, current)

    requirements.write_text(f"{second_dependency}\n", encoding="utf-8")
    module.write_text(
        "from candidate_dependency import VALUE\n"
        "def main(argv: list[str] | None = None) -> int:\n"
        "    return 0 if VALUE == 'two' else 1\n",
        encoding="utf-8",
    )

    replacement = install_release(source, releases, current)

    assert (current / "default").resolve() == replacement
    snapshots = list((releases / "default").glob("0.1.0-*"))
    assert len(snapshots) == 2
    assert replacement in snapshots


def test_release_final_snapshot_is_rechecked_after_publish(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = _release(tmp_path / "source")
    releases = tmp_path / "releases"
    current = tmp_path / "current"
    original_rename = Path.rename

    def mutate_published_snapshot(path: Path, destination: Path) -> Path:
        result = original_rename(path, destination)
        if ".tmp." in path.name:
            module = destination / "modules/sample.py"
            mode = stat.S_IMODE(module.stat().st_mode)
            module.chmod(mode | stat.S_IWUSR)
            module.write_text(
                "def main(argv: list[str] | None = None) -> int:\n    return 1\n",
                encoding="utf-8",
            )
            module.chmod(mode)
        return result

    monkeypatch.setattr(Path, "rename", mutate_published_snapshot)

    with pytest.raises(ValueError, match="final release snapshot changed"):
        install_release(source, releases, current)

    assert not (current / "default").exists()
    assert not list((releases / "default").glob("0.1.0-*"))


def test_release_snapshot_rejects_existing_digest_mismatch(tmp_path: Path) -> None:
    source = _release(tmp_path / "source")
    different = _release(tmp_path / "different")
    (different / "modules/sample.py").write_text(
        "def main(argv: list[str] | None = None) -> int:\n    return 1\n",
        encoding="utf-8",
    )
    releases = tmp_path / "releases"
    current = tmp_path / "current"
    validated = validate_release(source, validate_targets=False)
    other = validate_release(different, validate_targets=False)
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
    validated = validate_release(source, validate_targets=False)
    with pytest.raises(ValueError, match="child exited with status 1"):
        _validate_targets_in_child(
            validated,
            runtime_python=Path(sys.executable),
            runner_path=None,
        )


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
    release = validate_release(source, validate_targets=False)
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
    with pytest.raises(ValueError, match="snapshot name mismatch"):
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

    assert calls[-2][1:] == [
        "-m",
        "pip",
        "install",
        "--isolated",
        "--disable-pip-version-check",
        "--no-input",
        "--no-user",
        "--index-url",
        "https://pypi.org/simple",
        "PyYAML==6.0.3",
    ]


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


def test_runtime_site_package_and_support_package_edges(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    python = tmp_path / "venv/bin/python"
    with pytest.raises(ValueError, match="site-packages path is unavailable"):
        monkeypatch.setattr("atlas.runtime._run_stdout", lambda *args, **kwargs: "")
        _site_packages(python, {})

    import atlas.runtime as runtime_module

    monkeypatch.setattr(
        runtime_module,
        "__file__",
        str(tmp_path / "missing/src/atlas/runtime.py"),
    )
    with pytest.raises(ValueError, match="support package is unavailable"):
        _install_atlas_core(python, {})


def test_runtime_requirements_and_generation_root_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    missing_package = tmp_path / "missing-package"
    missing_package.mkdir()
    monkeypatch.setattr(runtime_module, "__file__", str(missing_package / "runtime.py"))
    with pytest.raises(ValueError, match="support requirements are unavailable"):
        _runtime_requirements(None)

    support = missing_package / "support-requirements.txt"
    support.write_text("\n", encoding="utf-8")
    with pytest.raises(ValueError, match="support requirements are empty"):
        _runtime_requirements(None)

    invalid_runtime = tmp_path / "invalid-runtime"
    invalid_runtime.mkdir()
    (invalid_runtime / "python").mkdir()
    (invalid_runtime / "python/envs").mkdir()
    generations = invalid_runtime / "python/envs/generations"
    generations.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ValueError, match="runtime generations path must be a directory"):
        _runtime_generations(invalid_runtime)

    generations.unlink()
    external = tmp_path / "external-generations"
    external.mkdir()
    generations.symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match="runtime generations path must be a directory"):
        _runtime_generations(invalid_runtime)


def test_selected_executable_rejects_invalid_resolver_result() -> None:
    with pytest.raises(TypeError, match="must return an ExecutableRef"):
        _selected_executable(lambda: object())


def test_runtime_support_package_rejects_existing_destination(tmp_path: Path) -> None:
    venv = tmp_path / "venv"
    site_packages = venv / "lib/python3.14/site-packages"
    (venv / "bin").mkdir(parents=True)
    site_packages.mkdir(parents=True)
    (site_packages / "atlas_core").mkdir()
    with pytest.raises(ValueError, match="destination already exists"):
        _install_atlas_core(venv / "bin/python", {})


def test_runtime_link_target_rejects_traversal_external_and_regular_entries(
    tmp_path: Path,
) -> None:
    environments = tmp_path / "runtime/python/envs"
    generations = environments / "generations"
    generations.mkdir(parents=True)
    active = environments / "scripts"
    assert _runtime_link_target(active, generations) is None

    active.symlink_to("../outside", target_is_directory=True)
    with pytest.raises(ValueError, match="path traversal"):
        _runtime_link_target(active, generations)

    active.unlink()
    active.symlink_to(Path("generations/missing"), target_is_directory=True)
    with pytest.raises(ValueError, match="not a generation"):
        _runtime_link_target(active, generations)

    active.unlink()
    external = tmp_path / "external"
    external.mkdir()
    active.symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match="path traversal"):
        _runtime_link_target(active, generations)

    active.unlink()
    active.write_text("not a runtime", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a directory or symlink"):
        _runtime_link_target(active, generations)


def test_generation_links_reject_invalid_targets_and_collect_old_generations(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    generations = root / "generations"
    active = root / "current"
    generations.mkdir(parents=True)

    with pytest.raises(ValueError, match="active artifact is missing"):
        active_generation(active, generations, label="artifact")

    active.write_text("not a link", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a symlink"):
        active_generation(active, generations, label="artifact")
    active.unlink()

    external = tmp_path / "external"
    external.mkdir()
    active.symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match="path traversal"):
        active_generation(active, generations, label="artifact")
    active.unlink()

    (generations / "chain").symlink_to(external, target_is_directory=True)
    active.symlink_to(Path("generations") / "chain", target_is_directory=True)
    with pytest.raises(ValueError, match="not a generation"):
        active_generation(active, generations, label="artifact")
    active.unlink()

    active.symlink_to(Path("../external"), target_is_directory=True)
    with pytest.raises(ValueError, match="path traversal"):
        active_generation(active, generations, label="artifact")
    active.unlink()

    new = generations / "new"
    old = generations / "old"
    new.mkdir()
    old.mkdir()
    active.symlink_to(Path("generations") / "new", target_is_directory=True)
    assert active_generation(active, generations, label="artifact") == new
    collect_generation_garbage(generations, active, label="artifact")
    assert new.is_dir()
    assert not old.exists()


def test_generation_gc_fails_closed_for_stale_or_unknown_lease_entries(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    generations = root / "generations"
    active = root / "current"
    generations.mkdir(parents=True)
    new = generations / "new"
    old = generations / "old"
    new.mkdir()
    old.mkdir()
    active.symlink_to(Path("generations") / "new", target_is_directory=True)
    leases = root / "leases"
    leases.mkdir()

    (leases / "stale.lease").write_text("old\n", encoding="utf-8")
    collect_generation_garbage(generations, active, label="artifact")
    assert not (leases / "stale.lease").exists()
    assert not old.exists()

    old.mkdir()
    (leases / "malformed.lease").write_text("\n", encoding="utf-8")
    collect_generation_garbage(generations, active, label="artifact")
    assert old.exists()
    assert (leases / "malformed.lease").exists()

    (leases / "malformed.lease").unlink()
    (leases / "unknown").write_text("old\n", encoding="utf-8")
    collect_generation_garbage(generations, active, label="artifact")
    assert old.exists()
    (leases / "unknown").unlink()
    collect_generation_garbage(generations, active, label="artifact")
    assert not old.exists()


def test_generation_lease_rejects_collisions_and_tolerates_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    generations = tmp_path / "generations"
    generations.mkdir()
    generation = generations / "one"
    generation.mkdir()
    leases = tmp_path / "leases"
    leases.mkdir()
    existing = leases / "lease-id.lease"
    existing.write_text("one\n", encoding="utf-8")
    with pytest.raises(ValueError, match="regular file"):
        with _generation_lease(generations, generation, "lease-id"):
            pass

    existing.unlink()
    original_remove = __import__("atlas.generations").generations.remove_path

    def fail_lease_cleanup(path: Path) -> None:
        if path.suffix == ".lease":
            raise OSError("lease cleanup failed")
        original_remove(path)

    monkeypatch.setattr("atlas.generations.remove_path", fail_lease_cleanup)
    with _generation_lease(generations, generation, "lease-id"):
        assert (leases / "lease-id.lease").exists()
    assert (leases / "lease-id.lease").exists()


def test_generation_lease_holds_runtime_and_artifact_generations(
    tmp_path: Path,
) -> None:
    runtime_generations = tmp_path / "runtime/generations"
    artifact_generations = tmp_path / "artifacts/generations"
    runtime_generation = runtime_generations / "runtime-old"
    artifact_generation = artifact_generations / "artifact-old"
    runtime_generation.mkdir(parents=True)
    artifact_generation.mkdir(parents=True)

    with generation_lease(
        runtime_generations,
        runtime_generation,
        artifact_generations,
        artifact_generation,
    ):
        assert list(runtime_generations.parent.joinpath("leases").glob("*.lease"))
        assert list(artifact_generations.parent.joinpath("leases").glob("*.lease"))
    assert not list(runtime_generations.parent.joinpath("leases").glob("*.lease"))
    assert not list(artifact_generations.parent.joinpath("leases").glob("*.lease"))


def test_child_generation_leases_follow_the_execution_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("ATLAS_RUNTIME_GENERATION", raising=False)
    monkeypatch.delenv("ATLAS_ARTIFACT_GENERATION", raising=False)
    with generation_lease_from_environment():
        pass

    runtime_generations = tmp_path / "runtime/python/envs/generations"
    artifact_generations = tmp_path / "artifacts/generations"
    runtime_generation = runtime_generations / "scripts.old"
    artifact_generation = artifact_generations / "artifact.old"
    runtime_generation.mkdir(parents=True)
    artifact_generation.mkdir(parents=True)
    monkeypatch.setenv("ATLAS_RUNTIME_GENERATION", str(runtime_generation))
    monkeypatch.setenv("ATLAS_ARTIFACT_GENERATION", str(artifact_generation))

    with generation_lease_from_environment():
        runtime_leases = runtime_generations.parent / "leases"
        artifact_leases = artifact_generations.parent / "leases"
        assert list(runtime_leases.glob("*.lease"))
        assert list(artifact_leases.glob("*.lease"))
    assert not list(runtime_leases.glob("*.lease"))
    assert not list(artifact_leases.glob("*.lease"))

    monkeypatch.delenv("ATLAS_ARTIFACT_GENERATION")
    with pytest.raises(ValueError, match="both required"):
        with generation_lease_from_environment():
            pass


def test_child_generation_lease_handoff_acknowledges_after_both_leases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_generations = tmp_path / "runtime/generations"
    artifact_generations = tmp_path / "artifacts/generations"
    runtime_generation = runtime_generations / "runtime-old"
    artifact_generation = artifact_generations / "artifact-old"
    runtime_generation.mkdir(parents=True)
    artifact_generation.mkdir(parents=True)
    monkeypatch.setenv("ATLAS_RUNTIME_GENERATION", str(runtime_generation))
    monkeypatch.setenv("ATLAS_ARTIFACT_GENERATION", str(artifact_generation))

    with _generation_lease_handoff(
        runtime_generations,
        runtime_generation,
        artifact_generations,
        artifact_generation,
    ) as handoff:
        ack_read, ack_write = os.pipe()
        inherited = (os.dup(handoff.runtime_fd), os.dup(handoff.artifact_fd), os.dup(ack_write))
        monkeypatch.setenv("ATLAS_LEASE_HANDOFF_RUNTIME_FD", str(inherited[0]))
        monkeypatch.setenv("ATLAS_LEASE_HANDOFF_ARTIFACT_FD", str(inherited[1]))
        monkeypatch.setenv("ATLAS_LEASE_HANDOFF_ACK_FD", str(inherited[2]))
        try:
            with generation_lease_from_environment():
                assert os.read(ack_read, 1) == b"1"
        finally:
            os.close(ack_read)
            os.close(ack_write)
    assert not list((runtime_generations.parent / "leases").glob("*.lease"))
    assert not list((artifact_generations.parent / "leases").glob("*.lease"))


def test_child_generation_lease_handoff_keeps_child_leases_after_acknowledged_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_generations = tmp_path / "runtime/generations"
    artifact_generations = tmp_path / "artifacts/generations"
    runtime_generation = runtime_generations / "runtime-old"
    artifact_generation = artifact_generations / "artifact-old"
    runtime_generation.mkdir(parents=True)
    artifact_generation.mkdir(parents=True)
    monkeypatch.setenv("ATLAS_RUNTIME_GENERATION", str(runtime_generation))
    monkeypatch.setenv("ATLAS_ARTIFACT_GENERATION", str(artifact_generation))

    with _generation_lease_handoff(
        runtime_generations,
        runtime_generation,
        artifact_generations,
        artifact_generation,
    ) as handoff:
        ack_read, ack_write = os.pipe()
        inherited = (os.dup(handoff.runtime_fd), os.dup(handoff.artifact_fd), os.dup(ack_write))
        for key, fd in zip(
            (
                "ATLAS_LEASE_HANDOFF_RUNTIME_FD",
                "ATLAS_LEASE_HANDOFF_ARTIFACT_FD",
                "ATLAS_LEASE_HANDOFF_ACK_FD",
            ),
            inherited,
            strict=True,
        ):
            monkeypatch.setenv(key, str(fd))
        try:
            with pytest.raises(RuntimeError, match="child failed"):
                with generation_lease_from_environment():
                    assert os.read(ack_read, 1) == b"1"
                    raise RuntimeError("child failed")
        finally:
            os.close(ack_read)
            os.close(ack_write)
    assert not list((runtime_generations.parent / "leases").glob("*.lease"))
    assert not list((artifact_generations.parent / "leases").glob("*.lease"))


def test_child_generation_lease_handoff_rejects_incomplete_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_generation = tmp_path / "runtime/generations/runtime-old"
    artifact_generation = tmp_path / "artifacts/generations/artifact-old"
    runtime_generation.mkdir(parents=True)
    artifact_generation.mkdir(parents=True)
    monkeypatch.setenv("ATLAS_RUNTIME_GENERATION", str(runtime_generation))
    monkeypatch.setenv("ATLAS_ARTIFACT_GENERATION", str(artifact_generation))
    monkeypatch.setenv("ATLAS_LEASE_HANDOFF_RUNTIME_FD", "3")
    with pytest.raises(ValueError, match="handoff variables are all required"):
        with generation_lease_from_environment():
            pass
    assert "ATLAS_LEASE_HANDOFF_RUNTIME_FD" not in os.environ


@pytest.mark.parametrize(
    "values",
    [
        ("not-an-integer", "4", "5"),
        ("3", "3", "5"),
        ("2", "4", "5"),
    ],
)
def test_child_generation_lease_handoff_rejects_invalid_descriptor_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    values: tuple[str, str, str],
) -> None:
    runtime_generation = tmp_path / "runtime/generations/runtime-old"
    artifact_generation = tmp_path / "artifacts/generations/artifact-old"
    runtime_generation.mkdir(parents=True)
    artifact_generation.mkdir(parents=True)
    monkeypatch.setenv("ATLAS_RUNTIME_GENERATION", str(runtime_generation))
    monkeypatch.setenv("ATLAS_ARTIFACT_GENERATION", str(artifact_generation))
    for key, value in zip(
        (
            "ATLAS_LEASE_HANDOFF_RUNTIME_FD",
            "ATLAS_LEASE_HANDOFF_ARTIFACT_FD",
            "ATLAS_LEASE_HANDOFF_ACK_FD",
        ),
        values,
        strict=True,
    ):
        monkeypatch.setenv(key, value)

    with pytest.raises(ValueError, match="descriptors"):
        with generation_lease_from_environment():
            pass
    assert not any(
        key in os.environ
        for key in (
            "ATLAS_LEASE_HANDOFF_RUNTIME_FD",
            "ATLAS_LEASE_HANDOFF_ARTIFACT_FD",
            "ATLAS_LEASE_HANDOFF_ACK_FD",
        )
    )


def test_child_generation_lease_handoff_requires_selections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ATLAS_RUNTIME_GENERATION", raising=False)
    monkeypatch.delenv("ATLAS_ARTIFACT_GENERATION", raising=False)
    read_fd, write_fd = os.pipe()
    inherited = (os.dup(read_fd), os.dup(write_fd), os.dup(read_fd))
    for key, value in zip(
        (
            "ATLAS_LEASE_HANDOFF_RUNTIME_FD",
            "ATLAS_LEASE_HANDOFF_ARTIFACT_FD",
            "ATLAS_LEASE_HANDOFF_ACK_FD",
        ),
        inherited,
        strict=True,
    ):
        monkeypatch.setenv(key, str(value))

    try:
        with pytest.raises(ValueError, match="selections are required"):
            with generation_lease_from_environment():
                pass
    finally:
        os.close(read_fd)
        os.close(write_fd)
    assert not any(
        key in os.environ
        for key in (
            "ATLAS_LEASE_HANDOFF_RUNTIME_FD",
            "ATLAS_LEASE_HANDOFF_ARTIFACT_FD",
            "ATLAS_LEASE_HANDOFF_ACK_FD",
        )
    )


@pytest.mark.parametrize(
    "invalid",
    ["nonregular", "wrong-path", "wrong-content", "ack", "closed", "closed-ack"],
)
def test_child_generation_lease_handoff_validates_descriptors_and_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    invalid: str,
) -> None:
    runtime_generations = tmp_path / "runtime/generations"
    artifact_generations = tmp_path / "artifacts/generations"
    runtime_generation = runtime_generations / "runtime-old"
    artifact_generation = artifact_generations / "artifact-old"
    runtime_generation.mkdir(parents=True)
    artifact_generation.mkdir(parents=True)
    runtime_selection = runtime_generation
    extra_fds: list[int] = []

    with _generation_lease_handoff(
        runtime_generations,
        runtime_generation,
        artifact_generations,
        artifact_generation,
    ) as handoff:
        ack_read, ack_write = os.pipe()
        runtime_fd = artifact_fd = ack_fd = -1
        try:
            if invalid == "nonregular":
                nonregular_read, nonregular_write = os.pipe()
                extra_fds.extend((nonregular_read, nonregular_write))
                runtime_fd = os.dup(nonregular_read)
            elif invalid == "wrong-path":
                wrong_path = tmp_path / "not-a-lease"
                wrong_path.write_text("runtime-old\n", encoding="utf-8")
                wrong_descriptor = os.open(wrong_path, os.O_RDWR)
                extra_fds.append(wrong_descriptor)
                runtime_fd = os.dup(wrong_descriptor)
            elif invalid == "wrong-content":
                runtime_selection = runtime_generations / "runtime-new"
                runtime_selection.mkdir()
                runtime_fd = os.dup(handoff.runtime_fd)
            elif invalid == "ack":
                ack_path = tmp_path / "ack-file"
                ack_descriptor = os.open(ack_path, os.O_RDWR | os.O_CREAT, 0o600)
                extra_fds.append(ack_descriptor)
                ack_fd = os.dup(ack_descriptor)
            else:
                runtime_fd = os.dup(handoff.runtime_fd)
            if runtime_fd < 0:
                runtime_fd = os.dup(handoff.runtime_fd)
            if artifact_fd < 0:
                artifact_fd = os.dup(handoff.artifact_fd)
            if ack_fd < 0:
                ack_fd = os.dup(ack_write)
            if invalid == "closed":
                os.close(runtime_fd)
            elif invalid == "closed-ack":
                os.close(ack_fd)
            for key, fd in (
                ("ATLAS_LEASE_HANDOFF_RUNTIME_FD", runtime_fd),
                ("ATLAS_LEASE_HANDOFF_ARTIFACT_FD", artifact_fd),
                ("ATLAS_LEASE_HANDOFF_ACK_FD", ack_fd),
            ):
                monkeypatch.setenv(key, str(fd))
            monkeypatch.setenv("ATLAS_RUNTIME_GENERATION", str(runtime_selection))
            monkeypatch.setenv("ATLAS_ARTIFACT_GENERATION", str(artifact_generation))
            with pytest.raises(ValueError):
                with generation_lease_from_environment():
                    pass
            if invalid not in {"ack", "closed-ack"}:
                assert os.read(ack_read, 1) == b"0"
        finally:
            os.close(ack_read)
            os.close(ack_write)
            for fd in extra_fds:
                os.close(fd)
    assert not list((runtime_generations.parent / "leases").glob("*.lease"))
    assert not list((artifact_generations.parent / "leases").glob("*.lease"))


@pytest.mark.parametrize("operation", ["readlink", "pread"])
def test_child_generation_lease_handoff_rejects_descriptor_io_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
) -> None:
    runtime_generations = tmp_path / "runtime/generations"
    artifact_generations = tmp_path / "artifacts/generations"
    runtime_generation = runtime_generations / "runtime-old"
    artifact_generation = artifact_generations / "artifact-old"
    runtime_generation.mkdir(parents=True)
    artifact_generation.mkdir(parents=True)
    monkeypatch.setattr(
        f"atlas_core.generations.os.{operation}",
        lambda *args: (_ for _ in ()).throw(OSError(f"{operation} failed")),
    )

    with _generation_lease_handoff(
        runtime_generations,
        runtime_generation,
        artifact_generations,
        artifact_generation,
    ) as handoff:
        ack_read, ack_write = os.pipe()
        inherited = (os.dup(handoff.runtime_fd), os.dup(handoff.artifact_fd), os.dup(ack_write))
        for key, fd in zip(
            (
                "ATLAS_LEASE_HANDOFF_RUNTIME_FD",
                "ATLAS_LEASE_HANDOFF_ARTIFACT_FD",
                "ATLAS_LEASE_HANDOFF_ACK_FD",
            ),
            inherited,
            strict=True,
        ):
            monkeypatch.setenv(key, str(fd))
        try:
            monkeypatch.setenv("ATLAS_RUNTIME_GENERATION", str(runtime_generation))
            monkeypatch.setenv("ATLAS_ARTIFACT_GENERATION", str(artifact_generation))
            with pytest.raises(ValueError, match="handoff descriptor"):
                with generation_lease_from_environment():
                    pass
            assert os.read(ack_read, 1) == b"0"
        finally:
            os.close(ack_read)
            os.close(ack_write)
    assert not list((runtime_generations.parent / "leases").glob("*.lease"))
    assert not list((artifact_generations.parent / "leases").glob("*.lease"))


def test_child_generation_lease_handoff_ignores_closed_ack_reader(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_generations = tmp_path / "runtime/generations"
    artifact_generations = tmp_path / "artifacts/generations"
    runtime_generation = runtime_generations / "runtime-old"
    artifact_generation = artifact_generations / "artifact-old"
    runtime_generation.mkdir(parents=True)
    artifact_generation.mkdir(parents=True)
    monkeypatch.setenv("ATLAS_RUNTIME_GENERATION", str(runtime_generation))
    monkeypatch.setenv("ATLAS_ARTIFACT_GENERATION", str(artifact_generation))

    with _generation_lease_handoff(
        runtime_generations,
        runtime_generation,
        artifact_generations,
        artifact_generation,
    ) as handoff:
        ack_read, ack_write = os.pipe()
        os.close(ack_read)
        inherited = (os.dup(handoff.runtime_fd), os.dup(handoff.artifact_fd), os.dup(ack_write))
        for key, fd in zip(
            (
                "ATLAS_LEASE_HANDOFF_RUNTIME_FD",
                "ATLAS_LEASE_HANDOFF_ARTIFACT_FD",
                "ATLAS_LEASE_HANDOFF_ACK_FD",
            ),
            inherited,
            strict=True,
        ):
            monkeypatch.setenv(key, str(fd))
        try:
            with generation_lease_from_environment():
                assert list((runtime_generations.parent / "leases").glob("*.lease"))
        finally:
            os.close(ack_write)
    assert not list((runtime_generations.parent / "leases").glob("*.lease"))
    assert not list((artifact_generations.parent / "leases").glob("*.lease"))


def test_child_generation_lease_handoff_requires_matching_parent_leases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_generations = tmp_path / "runtime/generations"
    artifact_generations = tmp_path / "artifacts/generations"
    runtime_generation = runtime_generations / "runtime-old"
    artifact_generation = artifact_generations / "artifact-old"
    runtime_generation.mkdir(parents=True)
    artifact_generation.mkdir(parents=True)
    monkeypatch.setenv("ATLAS_RUNTIME_GENERATION", str(runtime_generation))
    monkeypatch.setenv("ATLAS_ARTIFACT_GENERATION", str(artifact_generation))

    with _generation_lease(runtime_generations, runtime_generation, "runtime-lease") as runtime_fd:
        with _generation_lease(
            artifact_generations,
            artifact_generation,
            "artifact-lease",
        ) as artifact_fd:
            ack_read, ack_write = os.pipe()
            inherited = (os.dup(runtime_fd), os.dup(artifact_fd), os.dup(ack_write))
            for key, fd in zip(
                (
                    "ATLAS_LEASE_HANDOFF_RUNTIME_FD",
                    "ATLAS_LEASE_HANDOFF_ARTIFACT_FD",
                    "ATLAS_LEASE_HANDOFF_ACK_FD",
                ),
                inherited,
                strict=True,
            ):
                monkeypatch.setenv(key, str(fd))
            try:
                with pytest.raises(ValueError, match="do not match"):
                    with generation_lease_from_environment():
                        pass
                assert os.read(ack_read, 1) == b"0"
            finally:
                os.close(ack_read)
                os.close(ack_write)
    assert not list((runtime_generations.parent / "leases").glob("*.lease"))
    assert not list((artifact_generations.parent / "leases").glob("*.lease"))


def test_child_generation_lease_handoff_rejects_invalid_lease_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_generations = tmp_path / "runtime/generations"
    artifact_generations = tmp_path / "artifacts/generations"
    runtime_generation = runtime_generations / "runtime-old"
    artifact_generation = artifact_generations / "artifact-old"
    runtime_generation.mkdir(parents=True)
    artifact_generation.mkdir(parents=True)
    monkeypatch.setenv("ATLAS_RUNTIME_GENERATION", str(runtime_generation))
    monkeypatch.setenv("ATLAS_ARTIFACT_GENERATION", str(artifact_generation))

    with _generation_lease_handoff(
        runtime_generations,
        runtime_generation,
        artifact_generations,
        artifact_generation,
    ) as handoff:
        ack_read, ack_write = os.pipe()
        inherited = (os.dup(handoff.runtime_fd), os.dup(handoff.artifact_fd), os.dup(ack_write))
        for key, fd in zip(
            (
                "ATLAS_LEASE_HANDOFF_RUNTIME_FD",
                "ATLAS_LEASE_HANDOFF_ARTIFACT_FD",
                "ATLAS_LEASE_HANDOFF_ACK_FD",
            ),
            inherited,
            strict=True,
        ):
            monkeypatch.setenv(key, str(fd))
        leases = runtime_generations.parent / "leases"
        moved_leases = runtime_generations.parent / "leases.moved"
        leases.rename(moved_leases)
        leases.write_text("not a directory", encoding="utf-8")
        try:
            with pytest.raises(ValueError, match="leases path is invalid"):
                with generation_lease_from_environment():
                    pass
            assert os.read(ack_read, 1) == b"0"
        finally:
            leases.unlink()
            moved_leases.rename(leases)
            os.close(ack_read)
            os.close(ack_write)
    assert not list((runtime_generations.parent / "leases").glob("*.lease"))
    assert not list((artifact_generations.parent / "leases").glob("*.lease"))


def test_parent_generation_lease_handoff_timeout_cleans_parent_leases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_generations = tmp_path / "runtime/generations"
    artifact_generations = tmp_path / "artifacts/generations"
    runtime_generation = runtime_generations / "runtime-old"
    artifact_generation = artifact_generations / "artifact-old"
    runtime_generation.mkdir(parents=True)
    artifact_generation.mkdir(parents=True)
    terminated: list[object] = []
    monkeypatch.setattr("atlas.execution.select.select", lambda *args: ([], [], []))
    monkeypatch.setattr(
        "atlas.execution._terminate_process_group",
        lambda process: terminated.append(process),
    )

    class Child:
        pass

    child = Child()
    with pytest.raises(ValueError, match="did not acknowledge"):
        with _generation_lease_handoff(
            runtime_generations,
            runtime_generation,
            artifact_generations,
            artifact_generation,
        ):
            ack_read, ack_write = os.pipe()
            try:
                _await_generation_lease_ack(child, ack_read)
            finally:
                os.close(ack_read)
                os.close(ack_write)
    assert terminated == [child]
    assert not list((runtime_generations.parent / "leases").glob("*.lease"))
    assert not list((artifact_generations.parent / "leases").glob("*.lease"))


def test_child_generation_lease_handoff_cleans_partial_child_leases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_generations = tmp_path / "runtime/generations"
    artifact_generations = tmp_path / "artifacts/generations"
    runtime_generation = runtime_generations / "runtime-old"
    artifact_generation = artifact_generations / "artifact-old"
    runtime_generation.mkdir(parents=True)
    artifact_generation.mkdir(parents=True)
    monkeypatch.setenv("ATLAS_RUNTIME_GENERATION", str(runtime_generation))
    monkeypatch.setenv("ATLAS_ARTIFACT_GENERATION", str(artifact_generation))
    original_open = artifact_generations_module.os.open
    artifact_leases = artifact_generations.parent / "leases"

    def fail_artifact_lease(path, *args, **kwargs):
        if Path(path).parent == artifact_leases:
            raise OSError("artifact lease failed")
        return original_open(path, *args, **kwargs)

    with _generation_lease_handoff(
        runtime_generations,
        runtime_generation,
        artifact_generations,
        artifact_generation,
    ) as handoff:
        ack_read, ack_write = os.pipe()
        inherited = (os.dup(handoff.runtime_fd), os.dup(handoff.artifact_fd), os.dup(ack_write))
        for key, fd in zip(
            (
                "ATLAS_LEASE_HANDOFF_RUNTIME_FD",
                "ATLAS_LEASE_HANDOFF_ARTIFACT_FD",
                "ATLAS_LEASE_HANDOFF_ACK_FD",
            ),
            inherited,
            strict=True,
        ):
            monkeypatch.setenv(key, str(fd))
        monkeypatch.setattr(artifact_generations_module.os, "open", fail_artifact_lease)
        try:
            with pytest.raises(ValueError, match="generation lease must be a regular file"):
                with generation_lease_from_environment():
                    pass
            assert os.read(ack_read, 1) == b"0"
            assert len(list((runtime_generations.parent / "leases").glob("*.lease"))) == 1
            assert len(list(artifact_leases.glob("*.lease"))) == 1
        finally:
            os.close(ack_read)
            os.close(ack_write)
    assert not list((runtime_generations.parent / "leases").glob("*.lease"))
    assert not list(artifact_leases.glob("*.lease"))
@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("rejected", "rejected generation lease handoff"),
        ("select", "release child lease handoff failed"),
        ("read", "release child lease handoff failed"),
    ],
)
def test_parent_generation_lease_handoff_failure_cleans_parent_leases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
    message: str,
) -> None:
    runtime_generations = tmp_path / "runtime/generations"
    artifact_generations = tmp_path / "artifacts/generations"
    runtime_generation = runtime_generations / "runtime-old"
    artifact_generation = artifact_generations / "artifact-old"
    runtime_generation.mkdir(parents=True)
    artifact_generation.mkdir(parents=True)
    terminated: list[object] = []
    monkeypatch.setattr(
        "atlas.execution._terminate_process_group",
        lambda process: terminated.append(process),
    )

    class Child:
        pass

    child = Child()
    ack_read, ack_write = os.pipe()
    try:
        if failure == "rejected":
            os.write(ack_write, b"0")
        elif failure == "select":
            monkeypatch.setattr(
                "atlas.execution.select.select",
                lambda *args: (_ for _ in ()).throw(OSError("select failed")),
            )
        else:
            monkeypatch.setattr("atlas.execution.select.select", lambda *args: ([args[0][0]], [], []))
            monkeypatch.setattr(
                "atlas.execution.os.read",
                lambda *args: (_ for _ in ()).throw(OSError("read failed")),
            )
        with pytest.raises(ValueError, match=message):
            with _generation_lease_handoff(
                runtime_generations,
                runtime_generation,
                artifact_generations,
                artifact_generation,
            ):
                _await_generation_lease_ack(child, ack_read)
    finally:
        os.close(ack_read)
        os.close(ack_write)
    assert terminated == [child]
    assert not list((runtime_generations.parent / "leases").glob("*.lease"))
    assert not list((artifact_generations.parent / "leases").glob("*.lease"))


def test_child_generation_leases_fail_closed_and_preserve_lease_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    relative_generations = Path("relative/generations")
    with pytest.raises(ValueError, match="paths must be absolute"):
        with artifact_generation_lease(
            relative_generations,
            relative_generations / "one",
            "relative",
        ):
            pass

    invalid_root = tmp_path / "invalid-generations"
    invalid_root.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a directory"):
        with artifact_generation_lease(invalid_root, invalid_root, "invalid-root"):
            pass

    generations = tmp_path / "generations"
    generations.mkdir()
    generation = generations / "valid"
    generation.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ValueError, match="not a generation"):
        with artifact_generation_lease(generations, outside, "outside"):
            pass
    linked_generation = generations / "linked"
    linked_generation.symlink_to(generation, target_is_directory=True)
    with pytest.raises(ValueError, match="not a generation"):
        with artifact_generation_lease(generations, linked_generation, "linked"):
            pass

    leases = generations.parent / "leases"
    leases.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ValueError, match="leases path must be a directory"):
        with artifact_generation_lease(generations, generation, "bad-leases"):
            pass
    leases.unlink()
    leases.mkdir()
    collision = leases / "collision.lease"
    collision.write_text("valid\n", encoding="utf-8")
    with pytest.raises(ValueError, match="regular file"):
        with artifact_generation_lease(generations, generation, "collision"):
            pass
    collision.unlink()

    with artifact_generation_lease(generations, generation, "missing"):
        (leases / "missing.lease").unlink()

    original_unlink = artifact_generations_module.Path.unlink

    def fail_lease_unlink(path: Path, *args, **kwargs) -> None:
        if path.suffix == ".lease":
            raise OSError("lease cleanup failed")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(artifact_generations_module.Path, "unlink", fail_lease_unlink)
    with artifact_generation_lease(generations, generation, "stale"):
        pass
    assert (leases / "stale.lease").exists()


def test_generation_helpers_fail_closed_on_invalid_roots_targets_and_lease_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    active = tmp_path / "current"
    invalid_root = tmp_path / "invalid-generations"
    invalid_root.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ValueError, match="generations path must be a directory"):
        active_generation(active, invalid_root, label="artifact")
    with pytest.raises(ValueError, match="generation path must be a directory"):
        with _generation_lease(invalid_root, invalid_root, "invalid-root"):
            pass

    generations = tmp_path / "generations"
    generations.mkdir()
    generation = generations / "valid"
    generation.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ValueError, match="not a generation"):
        with _generation_lease(generations, outside, "invalid-target"):
            pass

    leases = tmp_path / "leases"
    leases.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ValueError, match="generation leases path must be a directory"):
        _leased_names(generations)
    with pytest.raises(ValueError, match="leases path must be a directory"):
        with _generation_lease(generations, generation, "invalid-leases"):
            pass

    leases.unlink()
    leases.mkdir()
    lease = leases / "locked.lease"
    lease.write_text("\n", encoding="utf-8")
    original_flock = generations_module.fcntl.flock

    def report_locked(_descriptor: int, flags: int) -> None:
        if flags & generations_module.fcntl.LOCK_NB:
            raise BlockingIOError
        original_flock(_descriptor, flags)

    monkeypatch.setattr(generations_module.fcntl, "flock", report_locked)
    names, safe = _leased_names(generations)
    assert names == set()
    assert safe is False

    lease.write_text("missing\n", encoding="utf-8")
    names, safe = _leased_names(generations)
    assert names == set()
    assert safe is False

    monkeypatch.setattr(
        generations_module.os,
        "open",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("open failed")),
    )
    lease.write_text("old\n", encoding="utf-8")
    names, safe = _leased_names(generations)
    assert names == set()
    assert safe is False


def test_remove_unleased_generation_is_lease_aware_and_candidate_scoped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    generations = tmp_path / "generations"
    generations.mkdir()
    active = generations / "active"
    active.mkdir()
    active_link = tmp_path / "current"
    active_link.symlink_to(Path("generations/active"), target_is_directory=True)
    candidate = generations / "candidate"
    candidate.mkdir()
    assert (
        _remove_unleased_generation(
            generations,
            candidate,
            active_link=active_link,
            label="artifact",
        )
        is True
    )
    assert not candidate.exists()
    assert _remove_unleased_generation(
        generations,
        candidate,
        active_link=active_link,
        label="artifact",
    ) is True
    assert _remove_unleased_generation(
        generations,
        active,
        active_link=active_link,
        label="artifact",
    ) is False

    held = generations / "held"
    held.mkdir()
    with _generation_lease(generations, held, "held"):
        assert _remove_unleased_generation(generations, held, label="artifact") is False
    assert held.is_dir()

    unsafe = generations / "unsafe"
    unsafe.mkdir()
    leases = generations.parent / "leases"
    (leases / "unrelated").write_text("not a lease", encoding="utf-8")
    assert _remove_unleased_generation(generations, unsafe, label="artifact") is False
    (leases / "unrelated").unlink()

    stale = generations / "stale"
    stale.mkdir()
    preexisting_lease = leases / "preexisting.lease"
    preexisting_lease.write_text("old\n", encoding="utf-8")
    assert _remove_unleased_generation(generations, stale, label="artifact") is False
    assert stale.is_dir()
    assert preexisting_lease.read_text(encoding="utf-8") == "old\n"
    preexisting_lease.unlink()

    cleanup_failure = generations / "cleanup-failure"
    cleanup_failure.mkdir()
    original_remove = __import__("atlas.launchers").launchers.remove_path
    monkeypatch.setattr(
        generations_module,
        "remove_path",
        lambda path: (
            (_ for _ in ()).throw(OSError("cleanup failed"))
            if path == cleanup_failure
            else original_remove(path)
        ),
    )
    assert _remove_unleased_generation(generations, cleanup_failure, label="artifact") is False
    assert cleanup_failure.is_dir()

    invalid_root = tmp_path / "invalid-root"
    invalid_root.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ValueError, match="generations path must be a directory"):
        _remove_unleased_generation(invalid_root, cleanup_failure, label="artifact")
    invalid_target = tmp_path / "invalid-target"
    invalid_target.mkdir()
    with pytest.raises(ValueError, match="not a generation"):
        _remove_unleased_generation(generations, invalid_target, label="artifact")
    nested = generations / "nested"
    nested.mkdir()
    with pytest.raises(ValueError, match="not a generation"):
        _remove_unleased_generation(generations, nested / "..", label="artifact")

def test_generation_gc_marks_lease_cleanup_failure_unsafe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    generations = tmp_path / "generations"
    generations.mkdir()
    leases = tmp_path / "leases"
    leases.mkdir()
    stale = leases / "stale.lease"
    stale.write_text("old\n", encoding="utf-8")
    original_remove = generations_module.remove_path

    def fail_lease_remove(path: Path) -> None:
        if path == stale:
            raise OSError("stale lease cleanup failed")
        original_remove(path)

    monkeypatch.setattr(generations_module, "remove_path", fail_lease_remove)
    names, safe = _leased_names(generations)
    assert names == set()
    assert safe is False
    assert stale.exists()


def test_generation_gc_handles_missing_roots_and_ignored_entries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing-generations"
    collect_generation_garbage(missing, tmp_path / "current", label="artifact")

    regular = tmp_path / "regular-generations"
    regular.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ValueError, match="generations path must be a directory"):
        collect_generation_garbage(regular, tmp_path / "current", label="artifact")

    generations = tmp_path / "generations"
    generations.mkdir()
    active = tmp_path / "current"
    selected = generations / "selected"
    selected.mkdir()
    (generations / ".temporary").mkdir()
    (generations / "file").write_text("ignored", encoding="utf-8")
    external = tmp_path / "external"
    external.mkdir()
    (generations / "link").symlink_to(external, target_is_directory=True)
    active.symlink_to(Path("generations") / "selected", target_is_directory=True)
    collect_generation_garbage(generations, active, label="artifact")
    assert selected.is_dir()
    assert (generations / ".temporary").is_dir()
    assert (generations / "file").is_file()
    assert (generations / "link").is_symlink()

    monkeypatch.setattr(
        generations_module,
        "remove_path",
        lambda path: (_ for _ in ()).throw(OSError("cleanup failed")),
    )
    old = generations / "old"
    old.mkdir()
    collect_generation_garbage(generations, active, label="artifact")
    assert old.is_dir()


def test_failed_transaction_cleanup_skips_removed_created_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = _release(tmp_path / "source")

    @contextmanager
    def fail_after_staging(runtime_root, python_version, release_roots, **kwargs):
        target = next(iter(release_roots))
        for item in [*target.rglob("*"), target]:
            item.chmod(stat.S_IMODE(item.stat().st_mode) | 0o200)
        shutil.rmtree(target)
        raise RuntimeError("candidate runtime failed")
        yield  # pragma: no cover - keeps this contextmanager syntactically complete

    monkeypatch.setattr("atlas.releases.prepared_runtime", fail_after_staging)
    with pytest.raises(RuntimeError, match="candidate runtime failed"):
        install_release(source, tmp_path / "releases", tmp_path / "current")


def test_runtime_install_rolls_back_existing_venv_when_final_rename_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_runtime(monkeypatch, tmp_path)
    runtime = tmp_path / "runtime"
    scripts = runtime / "python/envs/scripts"
    scripts.mkdir(parents=True)
    (scripts / "old.txt").write_text("old", encoding="utf-8")
    def fail_runtime_link(active: Path, target: Path) -> None:
        raise RuntimeError("rename failed")

    monkeypatch.setattr("atlas.runtime._replace_runtime_link", fail_runtime_link)
    with pytest.raises(RuntimeError, match="rename failed"):
        install_runtime(runtime, "3.12.3")

    assert (scripts / "old.txt").exists()


def test_runtime_install_leaves_no_venv_when_final_rename_fails_without_backup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_runtime(monkeypatch, tmp_path)
    runtime = tmp_path / "runtime"
    def fail_runtime_link(active: Path, target: Path) -> None:
        raise RuntimeError("rename failed")

    monkeypatch.setattr("atlas.runtime._replace_runtime_link", fail_runtime_link)
    with pytest.raises(RuntimeError, match="rename failed"):
        install_runtime(runtime, "3.12.3")

    assert not (runtime / "python/envs/scripts").exists()


def test_validate_release_targets_accepts_no_active_releases(tmp_path: Path) -> None:
    validate_release_targets([], runtime_python=tmp_path / "python")


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


def test_internal_execution_selection_is_snapshot_and_generation_bound(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    _set_env(monkeypatch, home, etc, var)
    paths = get_paths()
    ensure_dirs(paths)

    source = _release(tmp_path / "source", release_name="pinned")
    release = validate_release(source, validate_targets=False)
    snapshot = paths.releases_root / release.manifest.name / (
        f"{release.version}-{release.content_digest}"
    )
    snapshot.parent.mkdir(parents=True)
    shutil.copytree(source, snapshot)
    runtime_generation = paths.runtime / "python/envs/generations/scripts.old"
    artifact_generation = paths.artifact_root / "generations/artifact.old"
    (runtime_generation / "bin").mkdir(parents=True)
    (runtime_generation / "bin/python").write_text("python", encoding="utf-8")
    (artifact_generation / "python").mkdir(parents=True)
    (artifact_generation / "python/atlas_release_runner.py").write_text(
        "runner", encoding="utf-8"
    )
    pinned_values = {
        "ATLAS_EXECUTION_RELEASE_NAME": release.manifest.name,
        "ATLAS_EXECUTION_RELEASE_VERSION": release.version,
        "ATLAS_EXECUTION_RELEASE_DIGEST": release.content_digest,
        "ATLAS_EXECUTION_RELEASE_ROOT": str(snapshot),
        "ATLAS_EXECUTION_RUNTIME_GENERATION": str(runtime_generation),
        "ATLAS_EXECUTION_ARTIFACT_GENERATION": str(artifact_generation),
    }
    for name, value in pinned_values.items():
        monkeypatch.setenv(name, value)

    selected = pinned_execution_selection(paths)
    assert selected is not None
    assert selected.release.root == snapshot
    assert selected.runtime_generation == runtime_generation
    assert selected.artifact_generation == artifact_generation
    command = resolve_command_for_execution(paths, "sample")
    assert command.release == selected.release
    with pytest.raises(ValueError, match="unknown command"):
        resolve_command_for_execution(paths, "missing")
    snapshot_selection = _capture_generation_snapshot(paths, command, selected)
    assert snapshot_selection.runtime_generation == runtime_generation
    mismatched_release = replace(selected.release, name="other")
    with pytest.raises(ValueError, match="selection changed"):
        _capture_generation_snapshot(
            paths,
            command,
            replace(selected, release=mismatched_release),
        )

    monkeypatch.delenv("ATLAS_EXECUTION_RELEASE_DIGEST")
    with pytest.raises(ValueError, match="selection is incomplete"):
        pinned_execution_selection(paths)

    monkeypatch.setenv("ATLAS_EXECUTION_RELEASE_DIGEST", release.content_digest)
    monkeypatch.setenv("ATLAS_EXECUTION_RUNTIME_GENERATION", "relative-runtime")
    with pytest.raises(ValueError, match="not a concrete generation"):
        pinned_execution_selection(paths)

    monkeypatch.setenv("ATLAS_EXECUTION_RUNTIME_GENERATION", str(runtime_generation))
    runtime_link = paths.runtime / "python/envs/runtime-link"
    runtime_link.symlink_to(runtime_generation, target_is_directory=True)
    monkeypatch.setenv("ATLAS_EXECUTION_RUNTIME_GENERATION", str(runtime_link))
    with pytest.raises(ValueError, match="not a concrete generation"):
        pinned_execution_selection(paths)
    runtime_link.unlink()

    outside_runtime = tmp_path / "outside-runtime"
    (outside_runtime / "bin").mkdir(parents=True)
    monkeypatch.setenv("ATLAS_EXECUTION_RUNTIME_GENERATION", str(outside_runtime))
    with pytest.raises(ValueError, match="outside its generations directory"):
        pinned_execution_selection(paths)

    monkeypatch.setenv("ATLAS_EXECUTION_RUNTIME_GENERATION", str(runtime_generation))
    monkeypatch.setenv("ATLAS_EXECUTION_ARTIFACT_GENERATION", "relative-artifacts")
    with pytest.raises(ValueError, match="not a concrete generation"):
        pinned_execution_selection(paths)

    artifact_link = paths.artifact_root / "artifact-link"
    artifact_link.symlink_to(artifact_generation, target_is_directory=True)
    monkeypatch.setenv("ATLAS_EXECUTION_ARTIFACT_GENERATION", str(artifact_link))
    with pytest.raises(ValueError, match="not a concrete generation"):
        pinned_execution_selection(paths)
    artifact_link.unlink()

    outside_artifact = tmp_path / "outside-artifact"
    outside_artifact.mkdir()
    monkeypatch.setenv("ATLAS_EXECUTION_ARTIFACT_GENERATION", str(outside_artifact))
    with pytest.raises(ValueError, match="outside its generations directory"):
        pinned_execution_selection(paths)


def test_snapshot_catalog_rejects_untrusted_selection_metadata(tmp_path: Path) -> None:
    source = _release(tmp_path / "source", release_name="selected")
    release = validate_release(source, validate_targets=False)
    releases = tmp_path / "releases"
    snapshot = releases / "selected" / f"{release.version}-{release.content_digest}"
    snapshot.parent.mkdir(parents=True)
    shutil.copytree(source, snapshot)
    expected = {
        "expected_name": "selected",
        "expected_version": release.version,
        "expected_digest": release.content_digest,
    }
    assert release_from_snapshot(snapshot, releases, **expected).root == snapshot

    with pytest.raises(ValueError, match="paths must be absolute"):
        release_from_snapshot(Path("relative"), releases, **expected)
    with pytest.raises(ValueError, match="paths must be absolute"):
        release_from_snapshot(snapshot, Path("relative"), **expected)
    missing = tmp_path / "missing"
    with pytest.raises(ValueError, match="not a directory"):
        release_from_snapshot(missing, releases, **expected)
    snapshot_link = tmp_path / "snapshot-link"
    snapshot_link.symlink_to(snapshot, target_is_directory=True)
    with pytest.raises(ValueError, match="not a directory"):
        release_from_snapshot(snapshot_link, releases, **expected)
    releases_file = tmp_path / "releases-file"
    releases_file.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ValueError, match="releases root"):
        release_from_snapshot(snapshot, releases_file, **expected)
    releases_link = tmp_path / "releases-link"
    releases_link.symlink_to(releases, target_is_directory=True)
    with pytest.raises(ValueError, match="releases root"):
        release_from_snapshot(snapshot, releases_link, **expected)

    outside = tmp_path / "outside/selected" / snapshot.name
    outside.parent.mkdir(parents=True)
    shutil.copytree(source, outside)
    with pytest.raises(ValueError, match="outside its release directory"):
        release_from_snapshot(outside, releases, **expected)

    with pytest.raises(ValueError, match="identity changed"):
        release_from_snapshot(
            snapshot,
            releases,
            expected_name="selected",
            expected_version="9.9.9",
            expected_digest=release.content_digest,
        )
    wrong_snapshot = snapshot.parent / "wrong-name"
    shutil.copytree(source, wrong_snapshot)
    with pytest.raises(ValueError, match="snapshot name"):
        release_from_snapshot(
            wrong_snapshot,
            releases,
            **expected,
        )

    other_source = _release(tmp_path / "other-source", release_name="other")
    other = validate_release(other_source, validate_targets=False)
    manifest_mismatch = releases / "selected" / f"{other.version}-{other.content_digest}"
    shutil.copytree(other_source, manifest_mismatch)
    with pytest.raises(ValueError, match="does not match its manifest"):
        release_from_snapshot(
            manifest_mismatch,
            releases,
            expected_name="selected",
            expected_version=other.version,
            expected_digest=other.content_digest,
        )


def test_private_job_resolution_inherits_the_pinned_release(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    _set_env(monkeypatch, home, etc, var)
    paths = get_paths()
    ensure_dirs(paths)
    source = _release(tmp_path / "source", release_name="pinned-jobs")
    (source / "modules/collect.py").write_text(
        "def main(argv):\n    return 0\n",
        encoding="utf-8",
    )
    (source / "release.yml").write_text(
        "schema: atlas.release/v1\n"
        "name: pinned-jobs\n"
        "commands:\n"
        "  sample:\n"
        "    target: sample:main\n"
        "jobs:\n"
        "  collect:\n"
        "    target: collect:main\n",
        encoding="utf-8",
    )
    release = validate_release(source, validate_targets=False)
    snapshot = paths.releases_root / release.manifest.name / (
        f"{release.version}-{release.content_digest}"
    )
    snapshot.parent.mkdir(parents=True)
    shutil.copytree(source, snapshot)
    runtime_generation = paths.runtime / "python/envs/generations/scripts.old"
    artifact_generation = paths.artifact_root / "generations/artifact.old"
    runtime_generation.mkdir(parents=True)
    artifact_generation.mkdir(parents=True)
    for name, value in {
        "ATLAS_EXECUTION_RELEASE_NAME": release.manifest.name,
        "ATLAS_EXECUTION_RELEASE_VERSION": release.version,
        "ATLAS_EXECUTION_RELEASE_DIGEST": release.content_digest,
        "ATLAS_EXECUTION_RELEASE_ROOT": str(snapshot),
        "ATLAS_EXECUTION_RUNTIME_GENERATION": str(runtime_generation),
        "ATLAS_EXECUTION_ARTIFACT_GENERATION": str(artifact_generation),
    }.items():
        monkeypatch.setenv(name, value)

    calls: list[object] = []

    def fake_execute(paths_arg, resolver, args, **kwargs):
        calls.append(resolver())
        assert paths_arg == paths
        assert args == ["one"]
        return 37

    monkeypatch.setattr(jobs_module, "execute", fake_execute)
    assert jobs_module.run_job(paths, "pinned-jobs", "collect", ["one"]) == 37
    assert calls[0].release.root == snapshot
    with pytest.raises(ValueError, match="cannot change the selected release"):
        jobs_module.run_job(paths, "other", "collect", [])
    with pytest.raises(ValueError, match="unknown job"):
        jobs_module.run_job(paths, "pinned-jobs", "missing", [])

    paths.jobs_dir.mkdir(parents=True)
    (paths.jobs_dir / "pinned.yml").write_text(
        "schema: atlas.job-instance/v1\n"
        "release: pinned-jobs\n"
        "job: collect\n"
        "user: test\n"
        f"working_directory: {tmp_path}\n"
        "arguments: [one]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(jobs_module, "_validate_caller_user", lambda instance: None)
    assert jobs_module.run_job_instance(paths, "pinned") == 37
    (paths.jobs_dir / "other.yml").write_text(
        (paths.jobs_dir / "pinned.yml").read_text(encoding="utf-8").replace(
            "release: pinned-jobs", "release: other"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="cannot change the selected release"):
        jobs_module.run_job_instance(paths, "other")


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
    with pytest.raises(ValueError, match="path traversal"):
        publish_host_artifacts(paths)
    paths.artifact_current.unlink()
    paths.artifact_current.symlink_to(
        Path("generations/missing"),
        target_is_directory=True,
    )
    with pytest.raises(ValueError, match="target is not a generation"):
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


def test_host_artifact_publication_restores_previous_generation_and_launchers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    _set_env(monkeypatch, home, etc, var)
    paths = get_paths()
    ensure_dirs(paths)
    publish_host_artifacts(paths)
    previous_target = os.readlink(paths.artifact_current)
    previous_atlas = (paths.bin_dir / "atlas").read_bytes()
    previous_runner = paths.artifact_runner.read_bytes()

    def fail_launcher_write(*args, **kwargs) -> None:
        raise OSError("launcher write failed")

    monkeypatch.setattr("atlas.launchers._atomic_write", fail_launcher_write)
    with pytest.raises(OSError, match="launcher write failed"):
        publish_host_artifacts(paths)

    assert os.readlink(paths.artifact_current) == previous_target
    assert (paths.bin_dir / "atlas").read_bytes() == previous_atlas
    assert paths.artifact_runner.read_bytes() == previous_runner


def test_launcher_capture_and_restore_reject_unsafe_paths(tmp_path: Path) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(ValueError, match="host artifact must be a regular file"):
        _capture_launcher(directory)

    launcher = tmp_path / "launcher"
    launcher.write_bytes(b"old")
    state = _capture_launcher(launcher)
    assert state is not None
    launcher.write_bytes(b"new")
    _restore_launcher(launcher, state)
    assert launcher.read_bytes() == b"old"
    _restore_launcher(launcher, None)
    assert not launcher.exists()


def test_host_artifact_state_copy_rejects_special_files(tmp_path: Path) -> None:
    source = tmp_path / "fifo"
    os.mkfifo(source)
    with pytest.raises(ValueError, match="not a regular path"):
        _copy_state_entry(source, tmp_path / "backup/fifo")


def test_host_artifact_publication_reports_rollback_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    _set_env(monkeypatch, home, etc, var)
    paths = get_paths()
    ensure_dirs(paths)
    publish_host_artifacts(paths)

    monkeypatch.setattr(
        "atlas.launchers._atomic_write",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("launcher write failed")),
    )
    monkeypatch.setattr(
        "atlas.launchers._restore_current",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("current restore failed")),
    )
    with pytest.raises(RuntimeError, match="publication rollback failed"):
        publish_host_artifacts(paths)


def test_host_artifact_publication_continues_after_rollback_cleanup_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    _set_env(monkeypatch, home, etc, var)
    paths = get_paths()
    ensure_dirs(paths)
    (home / "lib/python").mkdir(parents=True)
    paths.shims.mkdir()

    original_remove = __import__("atlas.launchers").launchers.remove_path

    def fail_stable_restore(path: Path) -> None:
        if path in {home / "lib/python", paths.shims}:
            raise OSError("stable link restore failed")
        original_remove(path)

    monkeypatch.setattr("atlas.launchers.remove_path", fail_stable_restore)
    monkeypatch.setattr(
        "atlas.launchers._atomic_write",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("launcher write failed")),
    )
    with pytest.raises(RuntimeError, match="publication rollback failed"):
        publish_host_artifacts(paths)


def test_host_artifact_publication_leaves_candidate_when_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    _set_env(monkeypatch, home, etc, var)
    paths = get_paths()
    ensure_dirs(paths)
    original_remove = generations_module.remove_path

    def fail_candidate_cleanup(path: Path) -> None:
        if path.parent == paths.artifact_root / "generations" and not path.name.startswith("."):
            raise OSError("candidate cleanup failed")
        original_remove(path)

    monkeypatch.setattr(generations_module, "remove_path", fail_candidate_cleanup)
    monkeypatch.setattr(
        "atlas.launchers._atomic_write",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("launcher write failed")),
    )
    with pytest.raises(OSError, match="launcher write failed"):
        publish_host_artifacts(paths)
    candidates = [
        path
        for path in (paths.artifact_root / "generations").iterdir()
        if not path.name.startswith(".")
    ]
    assert candidates


def test_host_artifact_publication_reports_launcher_restore_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    _set_env(monkeypatch, home, etc, var)
    paths = get_paths()
    ensure_dirs(paths)
    publish_host_artifacts(paths)

    monkeypatch.setattr(
        "atlas.launchers._atomic_write",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("launcher write failed")),
    )
    monkeypatch.setattr(
        "atlas.launchers._restore_launcher",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("launcher restore failed")),
    )
    with pytest.raises(RuntimeError, match="publication rollback failed"):
        publish_host_artifacts(paths)


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
