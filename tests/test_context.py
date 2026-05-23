from __future__ import annotations

from pathlib import Path

from atlas_core.context import get_context


def test_get_context_from_env(monkeypatch, tmp_path: Path) -> None:
    host = tmp_path / "host.yml"
    host.write_text("name: test-host\nsite: kng01\n", encoding="utf-8")

    monkeypatch.setenv("ATLAS_HOME", str(tmp_path / "opt"))
    monkeypatch.setenv("ATLAS_ETC_DIR", str(tmp_path / "etc"))
    monkeypatch.setenv("ATLAS_VAR_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("ATLAS_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ATLAS_SCRIPTS_DIR", str(tmp_path / "scripts/current"))
    monkeypatch.setenv("ATLAS_HOST_FILE", str(host))
    monkeypatch.setenv("ATLAS_SCRIPT_NAME", "sample")
    monkeypatch.setenv("ATLAS_SCRIPT_RELEASE_NAME", "basic-scripts")
    monkeypatch.setenv("ATLAS_SCRIPT_VERSION", "2026.05.10-001")

    ctx = get_context()
    assert ctx.host.name == "test-host"
    assert ctx.script.name == "sample"
    assert ctx.script.release_name == "basic-scripts"
    assert ctx.script.version == "2026.05.10-001"
    assert ctx.script.release_root == tmp_path / "scripts/current"
    assert str(ctx.paths.home) == str(tmp_path / "opt")


def test_get_context_allows_env_mapping(monkeypatch, tmp_path: Path) -> None:
    host = tmp_path / "host.yml"
    host.write_text("name: test-host\n", encoding="utf-8")
    monkeypatch.setenv("ATLAS_HOST_FILE", str(host))

    ctx = get_context(
        env={
            "ATLAS_SCRIPT_NAME": "sample",
            "ATLAS_SCRIPT_RELEASE_NAME": "bundle-a",
            "ATLAS_SCRIPT_VERSION": "1.2.3",
            "ATLAS_SCRIPTS_DIR": str(tmp_path / "rel"),
        }
    )
    assert ctx.script.release_name == "bundle-a"
    assert ctx.script.release_root == tmp_path / "rel"


def test_context_to_dict(monkeypatch, tmp_path: Path) -> None:
    host = tmp_path / "host.yml"
    host.write_text("name: test-host\ntags: [a]\n", encoding="utf-8")

    monkeypatch.setenv("ATLAS_HOME", str(tmp_path / "opt"))
    monkeypatch.setenv("ATLAS_ETC_DIR", str(tmp_path / "etc"))
    monkeypatch.setenv("ATLAS_VAR_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("ATLAS_SCRIPTS_DIR", str(tmp_path / "scripts/current"))
    monkeypatch.setenv("ATLAS_HOST_FILE", str(host))
    monkeypatch.setenv("ATLAS_SCRIPT_NAME", "sample")

    data = get_context().to_dict()
    assert data["host"]["name"] == "test-host"
    assert data["host"]["tags"] == ["a"]
    assert data["paths"]["script_release_root"] == str(tmp_path / "scripts/current")
    assert data["paths"]["scripts"] == str(tmp_path / "scripts/current")
    assert data["script"]["release_root"] == str(tmp_path / "scripts/current")
