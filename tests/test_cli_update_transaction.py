from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path

import pytest

from atlas.cli import main
from atlas.paths import ensure_dirs, get_paths
from atlas.runtime import RuntimeCandidate


def _set_env(monkeypatch, home: Path, etc: Path, var: Path) -> None:
    monkeypatch.setenv("ATLAS_HOME", str(home))
    monkeypatch.setenv("ATLAS_ETC_DIR", str(etc))
    monkeypatch.setenv("ATLAS_VAR_DIR", str(var))
    monkeypatch.setenv("ATLAS_RUNTIME_DIR", str(home / "runtime"))
    if not (etc / "config.yml").exists():
        (etc / "config.yml").write_text(
            f"runtime:\n  python:\n    version: '{sys.version_info.major}.{sys.version_info.minor}'\n"
            "releases: {}\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(
        "atlas.runtime._ensure_pyenv_runtime",
        lambda version, env=None: Path(sys.executable),
    )
    runtime_python = home / "runtime/python/envs/scripts/bin/python"
    runtime_python.parent.mkdir(parents=True)
    runtime_python.symlink_to(Path(sys.executable))


def _write_release(path: Path, release_name: str, release_version: str, command_name: str) -> Path:
    (path / "modules").mkdir(parents=True, exist_ok=True)
    (path / "modules").mkdir(parents=True, exist_ok=True)
    (path / "VERSION").write_text(f"{release_version}\n", encoding="utf-8")
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


def _wheel(path: Path, package: str, version: str) -> Path:
    distribution = f"{package}-{version}"
    dist_info = f"{distribution}.dist-info"
    wheel = path / f"{distribution}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(f"{package}/__init__.py", "VALUE = 'available'\n")
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


def _path_state(path: Path):
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink():
        return ("symlink", os.readlink(path))
    mode = stat.S_IMODE(path.lstat().st_mode)
    if path.is_file():
        return ("file", mode, path.read_bytes())
    if path.is_dir():
        return (
            "directory",
            mode,
            tuple(
                (child.name, _path_state(child))
                for child in sorted(path.iterdir(), key=lambda item: item.name)
            ),
        )
    raise AssertionError(f"unsupported test path: {path}")


def _host_artifact_state(home: Path):
    paths = [
        home / "artifacts",
        home / "lib",
        home / "lib/python",
        home / "shims",
        home / "bin/atlas",
        home / "bin/artifact-runner",
        *(home / "lib").glob(".python.legacy.*"),
        *home.glob(".shims.legacy.*"),
    ]
    return tuple((path.relative_to(home).as_posix(), _path_state(path)) for path in paths)


def _fail_once_on_late_launcher_write(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    import atlas.launchers as launchers

    original = launchers._atomic_write
    writes = [0]

    def fail_late(path: Path, content: str) -> None:
        writes[0] += 1
        if writes[0] == 2:
            raise OSError("late launcher write")
        original(path, content)

    monkeypatch.setattr(launchers, "_atomic_write", fail_late)
    return writes


def test_release_update_rolls_back_all_target_releases_on_collision(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True, exist_ok=True)
    var.mkdir(parents=True, exist_ok=True)
    _set_env(monkeypatch, home, etc, var)

    old_one = _write_release(tmp_path / "old-one", "one", "0.1.0", "one-cmd")
    old_two = _write_release(tmp_path / "old-two", "two", "0.2.0", "two-cmd")
    assert main(["release", "install", str(old_one)]) == 0
    old_one_target = (home / "current/one").resolve()
    assert main(["release", "install", str(old_two)]) == 0
    old_two_target = (home / "current/two").resolve()
    assert (home / "current/one").resolve() == old_one_target
    assert (home / "current/two").resolve() == old_two_target

    new_one = _write_release(tmp_path / "new-one", "one", "0.3.0", "dup")
    new_two = _write_release(tmp_path / "new-two", "two", "0.4.0", "dup")
    (etc / "config.yml").write_text(
        f"""
runtime:
  python:
    version: "3.12.8"
releases:
  one:
    source: "file://{new_one}"
  two:
    source: "file://{new_two}"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    assert main(["release", "update"]) == 2
    assert "command name collision: dup found in releases: one, two" in capsys.readouterr().err

    assert (home / "current/one").resolve() == old_one_target
    assert (home / "current/two").resolve() == old_two_target
    assert main(["release", "list"]) == 0


def test_release_update_single_release_rolls_back_on_collision(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True, exist_ok=True)
    var.mkdir(parents=True, exist_ok=True)
    _set_env(monkeypatch, home, etc, var)

    old_one = _write_release(tmp_path / "old-one", "one", "0.1.0", "one-cmd")
    old_two = _write_release(tmp_path / "old-two", "two", "0.2.0", "two-cmd")
    assert main(["release", "install", str(old_one)]) == 0
    old_one_target = (home / "current/one").resolve()
    assert main(["release", "install", str(old_two)]) == 0
    old_two_target = (home / "current/two").resolve()
    assert (home / "current/one").resolve() == old_one_target
    assert (home / "current/two").resolve() == old_two_target

    new_one = _write_release(tmp_path / "new-one", "one", "0.3.0", "two-cmd")
    (etc / "config.yml").write_text(
        f"""
runtime:
  python:
    version: "3.12.8"
releases:
  one:
    source: "file://{new_one}"
  two:
    source: "file://{old_two}"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    assert main(["release", "update", "one"]) == 2
    assert (
        "command name collision: two-cmd found in releases: one, two"
        in capsys.readouterr().err
    )

    assert (home / "current/one").resolve() == old_one_target
    assert (home / "current/two").resolve() == old_two_target
    assert main(["release", "list"]) == 0


def test_release_install_restores_artifacts_without_republication(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True)
    _set_env(monkeypatch, home, etc, var)
    source = _write_release(tmp_path / "source", "sample", "0.1.0", "sample-show")
    monkeypatch.setattr(
        "atlas.cli._refresh_host_artifacts",
        lambda paths: (_ for _ in ()).throw(RuntimeError("refresh failed")),
    )

    assert main(["release", "install", str(source)]) == 2
    assert "refresh failed" in capsys.readouterr().err
    assert not (home / "current/sample").exists()


def test_release_install_reports_artifact_restore_failure(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True)
    _set_env(monkeypatch, home, etc, var)
    source = _write_release(tmp_path / "source", "sample", "0.1.0", "sample-show")
    monkeypatch.setattr(
        "atlas.cli._refresh_host_artifacts",
        lambda paths: (_ for _ in ()).throw(RuntimeError("refresh failed")),
    )
    monkeypatch.setattr(
        "atlas.cli.restore_host_artifact_state",
        lambda paths, state: (_ for _ in ()).throw(OSError("restore failed")),
    )

    assert main(["release", "install", str(source)]) == 2
    assert "release installation failed and host artifacts could not be restored" in capsys.readouterr().err


def test_runtime_install_validates_active_targets_with_the_configured_candidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys,
) -> None:
    versions_root = Path(sys.executable).parents[1].parent
    old_python = versions_root / "3.13.14/bin/python"
    new_python = versions_root / "3.14.6/bin/python"
    if not old_python.is_file() or not new_python.is_file():
        pytest.skip("Python 3.13.14 and 3.14.6 are required for this integration regression")

    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True)
    _set_env(monkeypatch, home, etc, var)
    monkeypatch.setattr(
        "atlas.runtime._ensure_pyenv_runtime",
        lambda version, env=None: {
            "3.13.14": old_python,
            "3.14.6": new_python,
        }[version],
    )

    dependency = _wheel(tmp_path, "conditional_dependency", "1.0.0")
    source = _write_release(tmp_path / "source", "sample", "0.1.0", "sample-show")
    (source / "requirements.txt").write_text(
        f"{dependency}; python_version < '3.14'\n",
        encoding="utf-8",
    )
    (source / "modules/sample_show.py").write_text(
        "from conditional_dependency import VALUE\n"
        "def main(argv: list[str] | None = None) -> int:\n"
        "    return 0 if VALUE == 'available' else 1\n",
        encoding="utf-8",
    )
    (etc / "config.yml").write_text(
        "runtime:\n  python:\n    version: '3.13.14'\nreleases: {}\n",
        encoding="utf-8",
    )

    assert main(["release", "install", str(source)]) == 0
    old_target = (home / "current/sample").resolve()
    old_runtime = (home / "runtime/python/envs/scripts").resolve()
    old_artifacts = (home / "artifacts/current").readlink()
    old_runtime_link = (home / "runtime/python/envs/scripts").readlink()

    (etc / "config.yml").write_text(
        "runtime:\n  python:\n    version: '3.14.6'\nreleases: {}\n",
        encoding="utf-8",
    )
    assert main(["runtime", "install"]) == 2
    error = capsys.readouterr().err

    assert "release target validation failed" in error
    assert "conditional_dependency" in error
    assert (home / "current/sample").resolve() == old_target
    assert (home / "runtime/python/envs/scripts").resolve() == old_runtime
    assert (home / "runtime/python/envs/scripts").readlink() == old_runtime_link
    assert (home / "artifacts/current").readlink() == old_artifacts
    assert list((home / "runtime/python/envs/generations").glob("scripts.*")) == [
        old_runtime
    ]


def test_release_install_final_validation_failure_does_not_publish_artifacts(
    monkeypatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True)
    _set_env(monkeypatch, home, etc, var)
    source = _write_release(tmp_path / "source", "sample", "0.1.0", "sample-show")
    validations: list[Path] = []
    refreshes: list[Path] = []

    def fail_validation(release, *, runtime_python, runner_path) -> None:
        validations.append(release.root)
        raise ValueError("final validation failed")

    monkeypatch.setattr("atlas.releases._validate_targets_in_child", fail_validation)
    monkeypatch.setattr(
        "atlas.cli._refresh_host_artifacts",
        lambda paths: refreshes.append(paths.artifact_root) or [],
    )

    assert main(["release", "install", str(source)]) == 2

    assert len(validations) == 1
    assert validations[0].parent == home / "releases/sample"
    assert validations[0].name.startswith("0.1.0-")
    assert refreshes == []
    assert not (home / "current/sample").exists()
    assert not list((home / "releases/sample").glob("0.1.0-*"))


def test_release_install_late_artifact_failure_restores_fresh_host_exactly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True)
    _set_env(monkeypatch, home, etc, var)
    paths = get_paths()
    ensure_dirs(paths)
    before = _host_artifact_state(home)
    source = _write_release(tmp_path / "source", "sample", "0.1.0", "sample-show")
    writes = _fail_once_on_late_launcher_write(monkeypatch)

    assert main(["release", "install", str(source)]) == 2
    assert writes == [2]

    assert _host_artifact_state(home) == before
    assert not (home / "current/sample").exists()
    assert not list((home / "artifacts/generations").iterdir())


def test_release_install_late_artifact_failure_restores_populated_host_exactly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True)
    _set_env(monkeypatch, home, etc, var)
    old_source = _write_release(tmp_path / "old", "sample", "0.1.0", "sample-show")
    assert main(["release", "install", str(old_source)]) == 0

    for path, mode in (
        (home / "bin/atlas", 0o741),
        (home / "bin/artifact-runner", 0o705),
    ):
        path.chmod(mode)
    before = _host_artifact_state(home)
    old_target = (home / "current/sample").resolve()
    old_runtime = (home / "runtime/python/envs/scripts").resolve()
    new_source = _write_release(tmp_path / "new", "sample", "0.2.0", "sample-show")
    writes = _fail_once_on_late_launcher_write(monkeypatch)

    assert main(["release", "install", str(new_source)]) == 2
    assert writes == [2]

    assert _host_artifact_state(home) == before
    assert (home / "current/sample").resolve() == old_target
    assert (home / "runtime/python/envs/scripts").resolve() == old_runtime
    assert not list((home / "releases/sample").glob("0.2.0-*"))


def test_late_artifact_failure_preserves_an_in_flight_child_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True)
    _set_env(monkeypatch, home, etc, var)
    old_source = _write_release(tmp_path / "old", "sample", "0.1.0", "sample-show")
    (old_source / "modules/nested.py").write_text(
        "from pathlib import Path\n"
        "import os\n"
        "def main(argv=None):\n"
        "    Path(os.environ['ATLAS_VAR_DIR'], 'nested-work').write_text('done')\n"
        "    return 0\n",
        encoding="utf-8",
    )
    (old_source / "release.yml").write_text(
        "schema: atlas.release/v1\n"
        "name: sample\n"
        "commands:\n"
        "  sample-show:\n"
        "    target: sample_show:main\n"
        "  nested:\n"
        "    target: nested:main\n",
        encoding="utf-8",
    )
    (old_source / "modules/sample_show.py").write_text(
        "import os\n"
        "import subprocess\n"
        "import time\n"
        "from pathlib import Path\n"
        "def main(argv=None):\n"
        "    var = Path(os.environ['ATLAS_VAR_DIR'])\n"
        "    (var / 'rollback-child-ready').write_text('ready')\n"
        "    while not (var / 'rollback-child-continue').exists():\n"
        "        time.sleep(0.01)\n"
        "    return subprocess.run(['nested'], check=False).returncode\n",
        encoding="utf-8",
    )
    assert main(["release", "install", str(old_source)]) == 0
    old_artifacts = (home / "artifacts/current").resolve()
    runner = (
        "from atlas.catalog import resolve_command\n"
        "from atlas.execution import execute\n"
        "from atlas.paths import get_paths\n"
        "paths = get_paths()\n"
        "command = resolve_command(paths.current_root, paths.releases_root, 'sample-show')\n"
        "raise SystemExit(execute(paths, command, []))\n"
    )
    repo_root = Path(__file__).resolve().parents[1]
    child_env = os.environ.copy()
    child_env["PYTHONPATH"] = os.pathsep.join(
        [str(repo_root / "src"), str(repo_root / "operations/modules")]
    )
    child = subprocess.Popen(
        [sys.executable, "-c", runner],
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready = var / "rollback-child-ready"
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        assert list((home / "artifacts/leases").glob("*.lease"))

        new_source = _write_release(tmp_path / "new", "sample", "0.2.0", "sample-show")
        writes = _fail_once_on_late_launcher_write(monkeypatch)
        assert main(["release", "install", str(new_source)]) == 2
        assert writes == [2]

        assert old_artifacts.is_dir()
        assert (home / "artifacts/current").resolve() == old_artifacts
        assert list((home / "artifacts/leases").glob("*.lease"))
        (var / "rollback-child-continue").write_text("continue", encoding="utf-8")
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 0, stderr
        assert stdout == ""
        assert (var / "nested-work").read_text(encoding="utf-8") == "done"
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)


def test_release_install_final_validation_failure_preserves_previous_state(
    monkeypatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True)
    _set_env(monkeypatch, home, etc, var)
    old_source = _write_release(tmp_path / "old", "sample", "0.1.0", "sample-show")
    assert main(["release", "install", str(old_source)]) == 0
    old_target = (home / "current/sample").resolve()
    old_artifacts = (home / "artifacts/current").resolve()

    new_source = _write_release(tmp_path / "new", "sample", "0.2.0", "sample-show")
    refreshes: list[Path] = []

    def fail_validation(release, *, runtime_python, runner_path) -> None:
        raise ValueError("final validation failed")

    monkeypatch.setattr("atlas.releases._validate_targets_in_child", fail_validation)
    monkeypatch.setattr(
        "atlas.cli._refresh_host_artifacts",
        lambda paths: refreshes.append(paths.artifact_root) or [],
    )

    assert main(["release", "install", str(new_source)]) == 2

    assert (home / "current/sample").resolve() == old_target
    assert (home / "artifacts/current").resolve() == old_artifacts
    assert refreshes == []
    assert not list((home / "releases/sample").glob("0.2.0-*"))


def test_release_update_final_validation_failure_does_not_publish_artifacts(
    monkeypatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True)
    _set_env(monkeypatch, home, etc, var)
    source = _write_release(tmp_path / "source", "sample", "0.1.0", "sample-show")
    (etc / "config.yml").write_text(
        "runtime:\n"
        "  python:\n"
        '    version: "3.14"\n'
        "releases:\n"
        f'  sample:\n    source: "{source}"\n',
        encoding="utf-8",
    )
    refreshes: list[Path] = []

    def fail_validation(release, *, runtime_python, runner_path) -> None:
        raise ValueError("final validation failed")

    monkeypatch.setattr("atlas.releases._validate_targets_in_child", fail_validation)
    monkeypatch.setattr(
        "atlas.cli._refresh_host_artifacts",
        lambda paths: refreshes.append(paths.artifact_root) or [],
    )

    assert main(["release", "update"]) == 2

    assert refreshes == []
    assert not (home / "current/sample").exists()
    assert not list((home / "releases/sample").glob("0.1.0-*"))


def test_release_update_restores_artifacts_without_republication(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True)
    _set_env(monkeypatch, home, etc, var)
    source = _write_release(tmp_path / "source", "sample", "0.1.0", "sample-show")
    (etc / "config.yml").write_text(
        "runtime:\n"
        "  python:\n"
        '    version: "3.12"\n'
        "releases:\n"
        "  sample:\n"
        f'    source: "{source}"\n'
        "    enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "atlas.cli._refresh_host_artifacts",
        lambda paths: (_ for _ in ()).throw(RuntimeError("refresh failed")),
    )

    assert main(["release", "update"]) == 2
    assert "refresh failed" in capsys.readouterr().err
    assert not (home / "current/sample").exists()


def test_release_update_reports_artifact_restore_failure(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True)
    _set_env(monkeypatch, home, etc, var)
    source = _write_release(tmp_path / "source", "sample", "0.1.0", "sample-show")
    (etc / "config.yml").write_text(
        "runtime:\n"
        "  python:\n"
        '    version: "3.12"\n'
        "releases:\n"
        "  sample:\n"
        f'    source: "{source}"\n'
        "    enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "atlas.cli._refresh_host_artifacts",
        lambda paths: (_ for _ in ()).throw(RuntimeError("refresh failed")),
    )
    monkeypatch.setattr(
        "atlas.cli.restore_host_artifact_state",
        lambda paths, state: (_ for _ in ()).throw(OSError("restore failed")),
    )

    assert main(["release", "update"]) == 2
    assert "release update failed and host artifacts could not be restored" in capsys.readouterr().err


def test_release_update_activates_enabled_release_and_refreshes_artifacts(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True)
    _set_env(monkeypatch, home, etc, var)
    source = _write_release(tmp_path / "source", "sample", "0.1.0", "sample-show")
    (etc / "config.yml").write_text(
        "runtime:\n"
        "  python:\n"
        '    version: "3.12"\n'
        "releases:\n"
        "  sample:\n"
        f'    source: "{source}"\n',
        encoding="utf-8",
    )

    assert main(["release", "update"]) == 0
    assert (home / "current/sample").is_symlink()
    assert (home / "shims/sample-show").is_symlink()
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "mutation",
    ["manifest_name", "version", "module_content"],
)
def test_release_update_rejects_source_identity_change_during_copy(
    monkeypatch,
    tmp_path: Path,
    capsys,
    mutation: str,
) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True)
    _set_env(monkeypatch, home, etc, var)

    prepared_count = 0

    @contextmanager
    def prepared(runtime_root, *args, **kwargs):
        nonlocal prepared_count
        prepared_count += 1
        root = runtime_root / "python/envs/generations" / f"scripts.test.{prepared_count}"
        (root / "bin").mkdir(parents=True, exist_ok=True)
        (root / "bin/python").symlink_to(Path(sys.executable))
        yield RuntimeCandidate(root=root, python=Path(sys.executable))

    monkeypatch.setattr("atlas.releases.prepared_runtime", prepared)

    old_source = _write_release(
        tmp_path / "old", "sample", "0.1.0", "sample-show"
    )
    assert main(["release", "install", str(old_source)]) == 0
    assert prepared_count == 1
    old_target = (home / "current/sample").resolve()
    before_artifacts = _host_artifact_state(home)
    before_releases = _path_state(home / "releases")

    source = _write_release(
        tmp_path / "source", "sample", "0.2.0", "sample-show"
    )
    (etc / "config.yml").write_text(
        "runtime:\n"
        f"  python:\n    version: '{sys.version_info.major}.{sys.version_info.minor}'\n"
        "releases:\n"
        f"  sample:\n    source: '{source}'\n",
        encoding="utf-8",
    )
    original_copytree = shutil.copytree

    def mutate_source_during_copy(source_root, destination, *args, **kwargs):
        if Path(source_root).resolve() == source.resolve():
            if mutation == "manifest_name":
                (source / "release.yml").write_text(
                    "schema: atlas.release/v1\n"
                    "name: injected\n"
                    "commands:\n"
                    "  sample-show:\n"
                    "    target: sample_show:main\n",
                    encoding="utf-8",
                )
            elif mutation == "version":
                (source / "VERSION").write_text("0.3.0\n", encoding="utf-8")
            elif mutation == "module_content":
                (source / "modules/sample_show.py").write_text(
                    "def main(argv: list[str] | None = None) -> int:\n"
                    "    return 1\n",
                    encoding="utf-8",
                )
            else:  # pragma: no cover - pytest parameterizes the supported mutations
                raise AssertionError(f"unsupported mutation: {mutation}")
        return original_copytree(source_root, destination, *args, **kwargs)

    monkeypatch.setattr("atlas.cli.shutil.copytree", mutate_source_during_copy)

    assert main(["release", "update", "sample"]) == 2
    assert "configured release changed during copy: sample" in capsys.readouterr().err
    assert prepared_count == 1
    assert (home / "current/sample").resolve() == old_target
    assert not (home / "current/injected").exists()
    assert not (home / "releases/injected").exists()
    assert _path_state(home / "releases") == before_releases
    assert _host_artifact_state(home) == before_artifacts


def test_release_update_handles_transaction_failure_before_activation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True)
    _set_env(monkeypatch, home, etc, var)
    source = _write_release(tmp_path / "source", "sample", "0.1.0", "sample-show")
    (etc / "config.yml").write_text(
        "runtime:\n"
        "  python:\n"
        f"    version: '{sys.version_info.major}.{sys.version_info.minor}'\n"
        "releases:\n"
        f"  sample:\n    source: '{source}'\n",
        encoding="utf-8",
    )

    @contextmanager
    def fail_transaction(*args, **kwargs):
        raise RuntimeError("transaction failed before activation")
        yield  # pragma: no cover - keeps this contextmanager syntactically complete

    monkeypatch.setattr("atlas.cli.reversible_release_transaction", fail_transaction)
    assert main(["release", "update"]) == 2
    assert not (home / "current/sample").exists()
