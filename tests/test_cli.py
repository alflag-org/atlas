from __future__ import annotations

from pathlib import Path

from atlas.cli import main


def test_install_list_which(monkeypatch, tmp_path: Path, capsys) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True, exist_ok=True)
    (etc / "host.yml").write_text("name: n1\n", encoding="utf-8")

    monkeypatch.setenv("ATLAS_HOME", str(home))
    monkeypatch.setenv("ATLAS_ETC_DIR", str(etc))
    monkeypatch.setenv("ATLAS_VAR_DIR", str(var))
    monkeypatch.setenv("ATLAS_RUNTIME_DIR", str(home / "runtime"))
    monkeypatch.setenv("ATLAS_SCRIPTS_DIR", str(home / "scripts/current"))

    release_src = Path("examples/scripts-release").resolve()
    assert main(["scripts", "install", str(release_src)]) == 0
    assert main(["scripts", "list"]) == 0
    out = capsys.readouterr().out
    assert "sample" in out
    assert "group-nested-sample" in out

    assert main(["which", "sample"]) == 0
    out2 = capsys.readouterr().out.strip()
    assert out2.endswith("/commands/sample.py")


def test_install_from_registry_alias(monkeypatch, tmp_path: Path, capsys) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True, exist_ok=True)
    (etc / "host.yml").write_text("name: n1\n", encoding="utf-8")
    release_src = Path("examples/scripts-release").resolve()
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

    monkeypatch.setenv("ATLAS_HOME", str(home))
    monkeypatch.setenv("ATLAS_ETC_DIR", str(etc))
    monkeypatch.setenv("ATLAS_VAR_DIR", str(var))
    monkeypatch.setenv("ATLAS_RUNTIME_DIR", str(home / "runtime"))
    monkeypatch.setenv("ATLAS_SCRIPTS_DIR", str(home / "scripts/current"))

    assert main(["scripts", "install", "sample-registry"]) == 0
    assert main(["scripts", "list"]) == 0
    out = capsys.readouterr().out
    assert "sample" in out


def test_runtime_status_prints_expanded_fields(monkeypatch, tmp_path: Path, capsys) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True, exist_ok=True)
    var.mkdir(parents=True, exist_ok=True)
    (etc / "config.yml").write_text(
        "runtime:\n  python:\n    version: '3.12.8'\nscripts:\n  source: 'dummy'\n", encoding="utf-8"
    )

    monkeypatch.setenv("ATLAS_HOME", str(home))
    monkeypatch.setenv("ATLAS_ETC_DIR", str(etc))
    monkeypatch.setenv("ATLAS_VAR_DIR", str(var))
    monkeypatch.setenv("ATLAS_RUNTIME_DIR", str(home / "runtime"))
    monkeypatch.setenv("ATLAS_SCRIPTS_DIR", str(home / "scripts/current"))

    monkeypatch.setattr(
        "atlas.cli.runtime_status",
        lambda runtime_root, python_version: {
            "provider": "pyenv",
            "configured_version": python_version or "",
            "provider_available": "true",
            "pyenv_python": "/opt/pyenv/versions/3.12.8/bin/python",
            "scripts_venv": "/opt/atlas/runtime/python/envs/scripts",
            "scripts_python": "/opt/atlas/runtime/python/envs/scripts/bin/python",
            "scripts_python_exists": "true",
        },
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
