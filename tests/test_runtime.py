from __future__ import annotations

from pathlib import Path

import pytest

from atlas.runtime import install_runtime, runtime_status


def test_runtime_install_requires_pyenv(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("atlas.runtime.shutil.which", lambda _: None)

    with pytest.raises(ValueError, match="pyenv command is required"):
        install_runtime(tmp_path / "runtime", "3.12.3")


def test_runtime_install_uses_pyenv_and_venv(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    runtime = tmp_path / "runtime"
    pyenv_root = tmp_path / "pyenv/versions/3.12.3"
    pyenv_python = pyenv_root / "bin/python"
    pyenv_python.parent.mkdir(parents=True)
    pyenv_python.write_text("", encoding="utf-8")

    monkeypatch.setattr("atlas.runtime.shutil.which", lambda _: "/usr/bin/pyenv")

    def fake_run(cmd: list[str], check: bool, capture_output: bool = False, text: bool = False):
        assert check is True
        calls.append(cmd)
        if capture_output:
            class Proc:
                stdout = f"{pyenv_root}\n"

            return Proc()
        return None

    monkeypatch.setattr("atlas.runtime.subprocess.run", fake_run)

    scripts_python = install_runtime(runtime, "3.12.3")

    assert scripts_python == runtime / "python/envs/scripts/bin/python"
    assert calls == [
        ["pyenv", "install", "-s", "3.12.3"],
        ["pyenv", "prefix", "3.12.3"],
        [str(pyenv_python), "-m", "venv", str(runtime / "python/envs/scripts")],
        [str(scripts_python), "-m", "pip", "install", "fire", "PyYAML"],
    ]


def test_runtime_status_includes_pyenv_provider(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("atlas.runtime.shutil.which", lambda _: "/usr/bin/pyenv")

    status = runtime_status(tmp_path / "runtime", "3.12.3")

    assert status["configured_version"] == "3.12.3"
    assert status["provider"] == "pyenv"
    assert status["provider_available"] == "true"
    assert status["python"] == "pyenv:3.12.3"
