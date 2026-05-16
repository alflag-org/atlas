from __future__ import annotations

from pathlib import Path

import pytest

from atlas.runtime import install_runtime, runtime_status


def test_runtime_install_requires_pyenv(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("atlas.runtime.shutil.which", lambda _: None)

    with pytest.raises(ValueError, match="pyenv command is required"):
        install_runtime(tmp_path / "runtime", "3.12.3")


def test_runtime_install_recreates_venv_and_installs_base_packages(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    runtime = tmp_path / "runtime"
    scripts_venv = runtime / "python/envs/scripts"
    pyenv_root = tmp_path / "pyenv/versions/3.12.3"
    pyenv_python = pyenv_root / "bin/python"
    pyenv_python.parent.mkdir(parents=True)
    pyenv_python.write_text("", encoding="utf-8")
    scripts_venv.mkdir(parents=True)
    marker = scripts_venv / "stale.txt"
    marker.write_text("stale", encoding="utf-8")

    monkeypatch.setattr("atlas.runtime.shutil.which", lambda _: "/usr/bin/pyenv")

    def fake_run(cmd: list[str], check: bool, capture_output: bool = False, text: bool = False):
        assert check is True
        calls.append(cmd)
        if capture_output:
            class Proc:
                stdout = f"{pyenv_root}\n"

            return Proc()
        if cmd == [str(pyenv_python), "-m", "venv", str(scripts_venv)]:
            scripts_venv.mkdir(parents=True, exist_ok=True)
            (scripts_venv / "bin").mkdir(parents=True, exist_ok=True)
            (scripts_venv / "bin/python").write_text("", encoding="utf-8")
        return None

    monkeypatch.setattr("atlas.runtime.subprocess.run", fake_run)

    scripts_python = install_runtime(runtime, "3.12.3")

    assert scripts_python == scripts_venv / "bin/python"
    assert not marker.exists()
    assert calls == [
        ["pyenv", "install", "-s", "3.12.3"],
        ["pyenv", "prefix", "3.12.3"],
        [str(pyenv_python), "-m", "venv", str(scripts_venv)],
        [str(scripts_python), "-m", "pip", "install", "--upgrade", "pip"],
        [str(scripts_python), "-m", "pip", "install", "fire", "PyYAML"],
    ]


def test_runtime_install_prefers_requirements_lock(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    runtime = tmp_path / "runtime"
    scripts_root = tmp_path / "scripts/current"
    scripts_root.mkdir(parents=True)
    (scripts_root / "requirements.lock").write_text("requests==2.32.3\n", encoding="utf-8")
    (scripts_root / "requirements.txt").write_text("click==8.1.7\n", encoding="utf-8")
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
        if cmd == [str(pyenv_python), "-m", "venv", str(runtime / "python/envs/scripts")]:
            scripts_python = runtime / "python/envs/scripts/bin/python"
            scripts_python.parent.mkdir(parents=True, exist_ok=True)
            scripts_python.write_text("", encoding="utf-8")
        return None

    monkeypatch.setattr("atlas.runtime.subprocess.run", fake_run)

    install_runtime(runtime, "3.12.3", scripts_root)

    assert calls[-1] == [
        str(runtime / "python/envs/scripts/bin/python"),
        "-m",
        "pip",
        "install",
        "fire",
        "PyYAML",
        "-r",
        str(scripts_root / "requirements.lock"),
    ]


def test_runtime_install_falls_back_to_requirements_txt(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    runtime = tmp_path / "runtime"
    scripts_root = tmp_path / "scripts/current"
    scripts_root.mkdir(parents=True)
    (scripts_root / "requirements.txt").write_text("click==8.1.7\n", encoding="utf-8")
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
        if cmd == [str(pyenv_python), "-m", "venv", str(runtime / "python/envs/scripts")]:
            scripts_python = runtime / "python/envs/scripts/bin/python"
            scripts_python.parent.mkdir(parents=True, exist_ok=True)
            scripts_python.write_text("", encoding="utf-8")
        return None

    monkeypatch.setattr("atlas.runtime.subprocess.run", fake_run)

    install_runtime(runtime, "3.12.3", scripts_root)

    assert calls[-1] == [
        str(runtime / "python/envs/scripts/bin/python"),
        "-m",
        "pip",
        "install",
        "fire",
        "PyYAML",
        "-r",
        str(scripts_root / "requirements.txt"),
    ]


def test_runtime_install_includes_requirements_from_all_active_releases(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    runtime = tmp_path / "runtime"
    common = tmp_path / "scripts/releases/common/0.1.0"
    kitsunebi = tmp_path / "scripts/releases/kitsunebi/0.2.0"
    common.mkdir(parents=True)
    kitsunebi.mkdir(parents=True)
    (common / "requirements.lock").write_text("requests==2.32.3\n", encoding="utf-8")
    (kitsunebi / "requirements.txt").write_text("click==8.1.7\n", encoding="utf-8")
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
        if cmd == [str(pyenv_python), "-m", "venv", str(runtime / "python/envs/scripts")]:
            scripts_python = runtime / "python/envs/scripts/bin/python"
            scripts_python.parent.mkdir(parents=True, exist_ok=True)
            scripts_python.write_text("", encoding="utf-8")
        return None

    monkeypatch.setattr("atlas.runtime.subprocess.run", fake_run)

    install_runtime(runtime, "3.12.3", [common, kitsunebi])

    assert calls[-1] == [
        str(runtime / "python/envs/scripts/bin/python"),
        "-m",
        "pip",
        "install",
        "fire",
        "PyYAML",
        "-r",
        str(common / "requirements.lock"),
        "-r",
        str(kitsunebi / "requirements.txt"),
    ]


def test_runtime_status_includes_pyenv_provider(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("atlas.runtime.shutil.which", lambda _: "/usr/bin/pyenv")
    monkeypatch.setattr("atlas.runtime._run_stdout", lambda cmd: str(tmp_path / "pyenv/versions/3.12.3"))
    scripts_python = tmp_path / "runtime/python/envs/scripts/bin/python"
    scripts_python.parent.mkdir(parents=True, exist_ok=True)
    scripts_python.write_text("", encoding="utf-8")

    status = runtime_status(tmp_path / "runtime", "3.12.3")

    assert status.configured_version == "3.12.3"
    assert status.provider == "pyenv"
    assert status.provider_available is True
    assert status.pyenv_python == tmp_path / "pyenv/versions/3.12.3/bin/python"
    assert status.scripts_venv == tmp_path / "runtime/python/envs/scripts"
    assert status.scripts_python == scripts_python
    assert status.scripts_python_exists is True


def test_runtime_status_does_not_fail_without_pyenv(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("atlas.runtime.shutil.which", lambda _: None)

    status = runtime_status(tmp_path / "runtime", "3.12.3")

    assert status.configured_version == "3.12.3"
    assert status.provider == "pyenv"
    assert status.provider_available is False
    assert status.pyenv_python is None


def test_runtime_status_does_not_fail_when_configured_pyenv_python_is_missing(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("atlas.runtime.shutil.which", lambda _: "/usr/bin/pyenv")

    def fake_run_stdout(cmd: list[str]) -> str:
        raise ValueError(f"{cmd[0]} command failed: {' '.join(cmd)}")

    monkeypatch.setattr("atlas.runtime._run_stdout", fake_run_stdout)

    status = runtime_status(tmp_path / "runtime", "3.12.3")

    assert status.provider_available is True
    assert status.pyenv_python is None
    assert status.pyenv_python_error == "pyenv command failed: pyenv prefix 3.12.3"
