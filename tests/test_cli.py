from __future__ import annotations

from pathlib import Path

import pytest

from atlas.cli import main
from atlas.runtime import RuntimeStatus


def _set_env(monkeypatch, home: Path, etc: Path, var: Path) -> None:
    monkeypatch.setenv("ATLAS_HOME", str(home))
    monkeypatch.setenv("ATLAS_ETC_DIR", str(etc))
    monkeypatch.setenv("ATLAS_VAR_DIR", str(var))
    monkeypatch.setenv("ATLAS_RUNTIME_DIR", str(home / "runtime"))
    monkeypatch.setenv("ATLAS_SCRIPTS_CURRENT_DIR", str(home / "scripts/current"))
    monkeypatch.setenv("ATLAS_SCRIPTS_DIR", str(home / "scripts/current"))


def test_install_list_which(monkeypatch, tmp_path: Path, capsys) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True, exist_ok=True)
    (etc / "host.yml").write_text("name: n1\n", encoding="utf-8")

    _set_env(monkeypatch, home, etc, var)

    release_src = Path("examples/basic-scripts-release").resolve()
    assert main(["scripts", "install", str(release_src)]) == 0
    assert main(["scripts", "list"]) == 0
    out = capsys.readouterr().out
    assert "sample" in out
    assert "group-nested-sample" in out

    assert main(["which", "sample"]) == 0
    out2 = capsys.readouterr().out.strip()
    assert out2.endswith("/commands/sample.py")


def test_update_list_verbose_and_status_with_multiple_releases(monkeypatch, tmp_path: Path, capsys) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True, exist_ok=True)
    (etc / "host.yml").write_text("name: n1\n", encoding="utf-8")
    release_src = Path("examples/basic-scripts-release").resolve()
    release_src2 = Path("examples/companion-scripts-release").resolve()
    (etc / "config.yml").write_text(
        f"""runtime:
  python:
    version: "3.12"
scripts:
  releases:
    sample:
      source: sample
    sample2:
      source: sample2
  registries:
    sample:
      source: "{release_src}"
    sample2:
      source: "{release_src2}"
""",
        encoding="utf-8",
    )

    _set_env(monkeypatch, home, etc, var)

    assert main(["scripts", "update"]) == 0
    assert main(["scripts", "list", "--verbose"]) == 0
    out = capsys.readouterr().out
    assert "sample\tsample\t2026.05.10-001" in out
    assert "sample2\tsample2\t0.2.0" in out

    assert main(["status"]) == 0
    status = capsys.readouterr().out
    assert f"scripts current root: {home / 'scripts/current'}" in status
    assert "active releases count: 2" in status
    assert "release: sample 2026.05.10-001" in status
    assert "release: sample2 0.2.0" in status


def test_install_from_registry_alias(monkeypatch, tmp_path: Path, capsys) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True, exist_ok=True)
    (etc / "host.yml").write_text("name: n1\n", encoding="utf-8")
    release_src = Path("examples/basic-scripts-release").resolve()
    (etc / "config.yml").write_text(
        f"""runtime:
  python:
    version: "3.12"
scripts:
  source: sample-registry
  registries:
    sample-registry:
      source: "{release_src}"
""",
        encoding="utf-8",
    )

    _set_env(monkeypatch, home, etc, var)

    assert main(["scripts", "install", "sample-registry"]) == 0
    assert main(["scripts", "list"]) == 0
    out = capsys.readouterr().out
    assert "sample" in out


def test_install_name_is_only_an_explicit_manifest_assertion(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True, exist_ok=True)
    _set_env(monkeypatch, home, etc, var)
    release_src = Path("examples/basic-scripts-release").resolve()

    assert main(["scripts", "install", str(release_src), "--name", "sample"]) == 0
    with pytest.raises(ValueError, match="release name mismatch: other != sample"):
        main(["scripts", "install", str(release_src), "--name", "other"])


def test_update_requires_configured_name_to_match_manifest(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True, exist_ok=True)
    release_src = Path("examples/basic-scripts-release").resolve()
    (etc / "config.yml").write_text(
        "runtime:\n"
        "  python:\n"
        '    version: "3.12"\n'
        "scripts:\n"
        "  releases:\n"
        "    other:\n"
        f'      source: "{release_src}"\n',
        encoding="utf-8",
    )
    _set_env(monkeypatch, home, etc, var)

    with pytest.raises(ValueError, match="release name mismatch: other != sample"):
        main(["scripts", "update"])


def test_runtime_status_prints_expanded_fields(monkeypatch, tmp_path: Path, capsys) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True, exist_ok=True)
    var.mkdir(parents=True, exist_ok=True)
    (etc / "config.yml").write_text(
        "runtime:\n  python:\n    version: '3.12.8'\nscripts:\n  source: 'dummy'\n", encoding="utf-8"
    )

    _set_env(monkeypatch, home, etc, var)

    monkeypatch.setattr(
        "atlas.cli.runtime_status",
        lambda runtime_root, python_version: RuntimeStatus(
            provider="pyenv",
            configured_version=python_version,
            provider_available=True,
            pyenv_python=Path("/opt/pyenv/versions/3.12.8/bin/python"),
            scripts_venv=Path("/opt/atlas/runtime/python/envs/scripts"),
            scripts_python=Path("/opt/atlas/runtime/python/envs/scripts/bin/python"),
            scripts_python_exists=True,
        ),
    )

    assert main(["runtime", "status"]) == 0
    out = capsys.readouterr().out
    assert "provider: pyenv" in out
    assert "configured version: 3.12.8" in out
    assert "provider available: true" in out
    assert "pyenv python: /opt/pyenv/versions/3.12.8/bin/python" in out
    assert "scripts venv: /opt/atlas/runtime/python/envs/scripts" in out
    assert "scripts python: /opt/atlas/runtime/python/envs/scripts/bin/python" in out
    assert "scripts python exists: true" in out
