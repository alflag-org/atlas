from __future__ import annotations

import sys
from pathlib import Path

import pytest

from atlas.cli import main
from atlas.runtime import RuntimeStatus


def _set_env(monkeypatch, home: Path, etc: Path, var: Path) -> None:
    monkeypatch.setenv("ATLAS_HOME", str(home))
    monkeypatch.setenv("ATLAS_ETC_DIR", str(etc))
    monkeypatch.setenv("ATLAS_VAR_DIR", str(var))
    monkeypatch.setenv("ATLAS_RUNTIME_DIR", str(home / "runtime"))
    runtime_python = home / "runtime/python/envs/scripts/bin/python"
    runtime_python.parent.mkdir(parents=True)
    runtime_python.symlink_to(Path(sys.executable))


def test_install_list_commands_and_which(monkeypatch, tmp_path: Path, capsys) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True)
    (etc / "host.yml").write_text("name: n1\n", encoding="utf-8")
    _set_env(monkeypatch, home, etc, var)

    release_src = Path("examples/basic-release").resolve()
    assert main(["release", "install", str(release_src)]) == 0
    assert main(["release", "list"]) == 0
    assert "sample" in capsys.readouterr().out

    assert main(["command", "list"]) == 0
    commands = capsys.readouterr().out
    assert "sample" in commands
    assert "group-nested-sample" in commands

    assert main(["which", "sample"]) == 0
    assert capsys.readouterr().out.strip() == "sample_command:main"


def test_update_list_verbose_and_status_with_multiple_releases(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True)
    (etc / "host.yml").write_text("name: n1\n", encoding="utf-8")
    release_src = Path("examples/basic-release").resolve()
    release_src2 = Path("examples/companion-release").resolve()
    (etc / "config.yml").write_text(
        f"""runtime:
  python:
    version: "3.12"
releases:
  sample:
    source: "{release_src}"
  sample2:
    source: "{release_src2}"
""",
        encoding="utf-8",
    )
    _set_env(monkeypatch, home, etc, var)

    assert main(["release", "update"]) == 0
    assert main(["release", "list", "--verbose"]) == 0
    releases = capsys.readouterr().out
    assert "sample\t2026.05.10-001" in releases
    assert "sample2\t0.2.0" in releases

    assert main(["command", "list", "--verbose"]) == 0
    commands = capsys.readouterr().out
    assert "sample\tsample\t2026.05.10-001" in commands
    assert "sample2\tsample2\t0.2.0" in commands

    assert main(["status"]) == 0
    status = capsys.readouterr().out
    assert f"releases root: {home / 'releases'}" in status
    assert f"current root: {home / 'current'}" in status
    assert f"artifact runner: {home / 'bin/artifact-runner'}" in status
    assert "active releases count: 2" in status
    assert "release: sample 2026.05.10-001" in status
    assert "release: sample2 0.2.0" in status
    assert "services count: 0" in status


def test_legacy_cli_and_install_name_are_not_registered(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True)
    _set_env(monkeypatch, home, etc, var)
    release_src = Path("examples/basic-release").resolve()

    with pytest.raises(SystemExit) as scripts_error:
        main(["scripts", "install", str(release_src)])
    assert scripts_error.value.code == 2

    with pytest.raises(SystemExit) as name_error:
        main(["release", "install", str(release_src), "--name", "sample"])
    assert name_error.value.code == 2


def test_update_requires_configured_name_to_match_manifest(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True)
    release_src = Path("examples/basic-release").resolve()
    (etc / "config.yml").write_text(
        "runtime:\n"
        "  python:\n"
        '    version: "3.12"\n'
        "releases:\n"
        "  other:\n"
        f'    source: "{release_src}"\n',
        encoding="utf-8",
    )
    _set_env(monkeypatch, home, etc, var)

    assert main(["release", "update"]) == 2
    assert "configured release name mismatch: other != sample" in capsys.readouterr().err


def test_runtime_status_prints_expanded_fields(monkeypatch, tmp_path: Path, capsys) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True)
    var.mkdir(parents=True)
    (etc / "config.yml").write_text(
        "runtime:\n  python:\n    version: '3.12.8'\nreleases: {}\n",
        encoding="utf-8",
    )
    _set_env(monkeypatch, home, etc, var)

    monkeypatch.setattr(
        "atlas.cli.runtime_status",
        lambda runtime_root, python_version: RuntimeStatus(
            provider="pyenv",
            configured_version=python_version,
            provider_available=True,
            pyenv_python=Path("/opt/pyenv/versions/3.12.8/bin/python"),
            artifacts_venv=Path("/opt/atlas/runtime/python/envs/scripts"),
            runtime_python=Path("/opt/atlas/runtime/python/envs/scripts/bin/python"),
            runtime_python_exists=True,
        ),
    )

    assert main(["runtime", "status"]) == 0
    out = capsys.readouterr().out
    assert "provider: pyenv" in out
    assert "configured version: 3.12.8" in out
    assert "provider available: true" in out
    assert "pyenv python: /opt/pyenv/versions/3.12.8/bin/python" in out
    assert "artifacts venv: /opt/atlas/runtime/python/envs/scripts" in out
    assert "runtime python: /opt/atlas/runtime/python/envs/scripts/bin/python" in out
    assert "runtime python exists: true" in out
