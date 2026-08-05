from __future__ import annotations

import sys
from pathlib import Path

from atlas.cli import main


def _set_env(monkeypatch, home: Path, etc: Path, var: Path) -> None:
    monkeypatch.setenv("ATLAS_HOME", str(home))
    monkeypatch.setenv("ATLAS_ETC_DIR", str(etc))
    monkeypatch.setenv("ATLAS_VAR_DIR", str(var))
    monkeypatch.setenv("ATLAS_RUNTIME_DIR", str(home / "runtime"))
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


def test_release_install_reports_failed_host_artifact_restore(
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
    assert (
        "release installation failed and host artifacts could not be restored"
        in capsys.readouterr().err
    )
    assert not (home / "current/sample").exists()


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


def test_release_update_reports_failed_host_artifact_restore(
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
    assert (
        "release update failed and host artifacts could not be restored"
        in capsys.readouterr().err
    )
    assert not (home / "current/sample").exists()


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
