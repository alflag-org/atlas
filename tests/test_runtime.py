from __future__ import annotations

import os
import stat
import subprocess
import sys
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
            target = Path(cmd[3])
            target.mkdir(parents=True, exist_ok=True)
            (target / "bin").mkdir(parents=True, exist_ok=True)
            (target / "bin/python").write_text("", encoding="utf-8")
        return None

    monkeypatch.setattr("atlas.runtime.subprocess.run", fake_run)

    scripts_python = install_runtime(runtime, "3.12.3")

    assert scripts_python == scripts_venv / "bin/python"
    assert not marker.exists()
    assert calls[0:2] == [
        ["pyenv", "install", "-s", "3.12.3"],
        ["pyenv", "prefix", "3.12.3"],
    ]
    assert calls[2][:3] == [str(pyenv_python), "-m", "venv"]
    assert calls[3][0].endswith("/bin/python")
    assert calls[3][1:] == ["-m", "pip", "install", "--upgrade", "pip"]
    assert calls[4][0].endswith("/bin/python")
    assert calls[4][1:] == ["-m", "pip", "install", "fire", "PyYAML"]
    assert calls[5][0].endswith("/bin/python")
    assert calls[5][1:] == ["-m", "pip", "check"]


def test_runtime_install_sets_default_temp_environment(monkeypatch, tmp_path: Path) -> None:
    envs: list[dict[str, str]] = []
    runtime = tmp_path / "opt/atlas/runtime"
    atlas_tmp = tmp_path / "opt/atlas/tmp"
    build_cache = tmp_path / "var/lib/atlas/cache/python-build"
    pyenv_root = tmp_path / "pyenv/versions/3.12.3"
    pyenv_python = pyenv_root / "bin/python"
    pyenv_python.parent.mkdir(parents=True)
    pyenv_python.write_text("", encoding="utf-8")
    monkeypatch.delenv("TMPDIR", raising=False)
    monkeypatch.delenv("PYTHON_BUILD_CACHE_PATH", raising=False)
    monkeypatch.setattr("atlas.runtime.shutil.which", lambda _: "/usr/bin/pyenv")

    def fake_run(
        cmd: list[str],
        check: bool,
        capture_output: bool = False,
        text: bool = False,
        env: dict[str, str] | None = None,
    ):
        assert env is not None
        envs.append(env)
        if capture_output:
            class Proc:
                stdout = f"{pyenv_root}\n"

            return Proc()
        if cmd[:3] == [str(pyenv_python), "-m", "venv"]:
            scripts_python = Path(cmd[3]) / "bin/python"
            scripts_python.parent.mkdir(parents=True, exist_ok=True)
            scripts_python.write_text("", encoding="utf-8")
        return None

    monkeypatch.setattr("atlas.runtime.subprocess.run", fake_run)

    install_runtime(runtime, "3.12.3", tmp_dir=atlas_tmp, python_build_cache_path=build_cache)

    assert envs
    assert all(env["TMPDIR"] == str(atlas_tmp) for env in envs)
    assert all(env["PYTHON_BUILD_CACHE_PATH"] == str(build_cache) for env in envs)
    assert atlas_tmp.is_dir()
    assert build_cache.is_dir()


def test_runtime_install_respects_explicit_tmpdir(monkeypatch, tmp_path: Path) -> None:
    envs: list[dict[str, str]] = []
    runtime = tmp_path / "opt/atlas/runtime"
    explicit_tmp = tmp_path / "explicit-tmp"
    atlas_tmp = tmp_path / "opt/atlas/tmp"
    build_cache = tmp_path / "var/lib/atlas/cache/python-build"
    pyenv_root = tmp_path / "pyenv/versions/3.12.3"
    pyenv_python = pyenv_root / "bin/python"
    pyenv_python.parent.mkdir(parents=True)
    pyenv_python.write_text("", encoding="utf-8")
    monkeypatch.setenv("TMPDIR", str(explicit_tmp))
    monkeypatch.delenv("PYTHON_BUILD_CACHE_PATH", raising=False)
    monkeypatch.setattr("atlas.runtime.shutil.which", lambda _: "/usr/bin/pyenv")

    def fake_run(
        cmd: list[str],
        check: bool,
        capture_output: bool = False,
        text: bool = False,
        env: dict[str, str] | None = None,
    ):
        assert env is not None
        envs.append(env)
        if capture_output:
            class Proc:
                stdout = f"{pyenv_root}\n"

            return Proc()
        if cmd[:3] == [str(pyenv_python), "-m", "venv"]:
            scripts_python = Path(cmd[3]) / "bin/python"
            scripts_python.parent.mkdir(parents=True, exist_ok=True)
            scripts_python.write_text("", encoding="utf-8")
        return None

    monkeypatch.setattr("atlas.runtime.subprocess.run", fake_run)

    install_runtime(runtime, "3.12.3", tmp_dir=atlas_tmp, python_build_cache_path=build_cache)

    assert envs
    assert all(env["TMPDIR"] == str(explicit_tmp) for env in envs)
    assert explicit_tmp.is_dir()
    assert not atlas_tmp.exists()


def test_runtime_install_keeps_console_scripts_relocatable_and_executable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    original_run = subprocess.run
    runtime = tmp_path / "runtime"
    scripts_venv = runtime / "python/envs/scripts"
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
        if capture_output:
            class Proc:
                stdout = f"{pyenv_root}\n"

            return Proc()
        if cmd[:3] == [str(pyenv_python), "-m", "venv"]:
            scripts_python = Path(cmd[3]) / "bin/python"
            scripts_python.parent.mkdir(parents=True, exist_ok=True)
            scripts_python.symlink_to(sys.executable)
        if cmd[1:] == ["-m", "pip", "install", "fire", "PyYAML"]:
            console_script = Path(cmd[0]).parent / "atlas-sample"
            console_script.write_text(f"#!{cmd[0]}\nprint('console ok')\n", encoding="utf-8")
            console_script.chmod(console_script.stat().st_mode | stat.S_IXUSR)
        return None

    monkeypatch.setattr("atlas.runtime.subprocess.run", fake_run)

    install_runtime(runtime, "3.12.3")

    console_script = scripts_venv / "bin/atlas-sample"
    shebang = console_script.read_text(encoding="utf-8").splitlines()[0]
    assert shebang == f"#!{scripts_venv / 'bin/python'}"
    assert "scripts.tmp." not in shebang
    proc = original_run([str(console_script)], check=True, capture_output=True, text=True)
    assert proc.stdout == "console ok\n"


def test_runtime_install_rejects_stale_temp_shebang(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
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
        if capture_output:
            class Proc:
                stdout = f"{pyenv_root}\n"

            return Proc()
        if cmd[:3] == [str(pyenv_python), "-m", "venv"]:
            scripts_python = Path(cmd[3]) / "bin/python"
            scripts_python.parent.mkdir(parents=True, exist_ok=True)
            scripts_python.write_text("", encoding="utf-8")
        if cmd[1:] == ["-m", "pip", "install", "fire", "PyYAML"]:
            stale_python = runtime / "python/envs" / f"scripts.tmp.{os.getpid()}" / "bin/python"
            console_script = Path(cmd[0]).parent / "atlas-stale"
            console_script.write_text(f"#!{stale_python}\nprint('stale')\n", encoding="utf-8")
            console_script.chmod(console_script.stat().st_mode | stat.S_IXUSR)
        return None

    monkeypatch.setattr("atlas.runtime.subprocess.run", fake_run)

    with pytest.raises(ValueError, match="console script shebang must point to"):
        install_runtime(runtime, "3.12.3")

    assert not (runtime / "python/envs/scripts").exists()


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
            scripts_python = Path(cmd[3]) / "bin/python"
            scripts_python.parent.mkdir(parents=True, exist_ok=True)
            scripts_python.write_text("", encoding="utf-8")
        return None

    monkeypatch.setattr("atlas.runtime.subprocess.run", fake_run)

    install_runtime(runtime, "3.12.3", scripts_root)

    assert calls[-2][0].endswith("/bin/python")
    assert calls[-2][1:] == [
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
            scripts_python = Path(cmd[3]) / "bin/python"
            scripts_python.parent.mkdir(parents=True, exist_ok=True)
            scripts_python.write_text("", encoding="utf-8")
        return None

    monkeypatch.setattr("atlas.runtime.subprocess.run", fake_run)

    install_runtime(runtime, "3.12.3", scripts_root)

    assert calls[-2][0].endswith("/bin/python")
    assert calls[-2][1:] == [
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
            scripts_python = Path(cmd[3]) / "bin/python"
            scripts_python.parent.mkdir(parents=True, exist_ok=True)
            scripts_python.write_text("", encoding="utf-8")
        return None

    monkeypatch.setattr("atlas.runtime.subprocess.run", fake_run)

    install_runtime(runtime, "3.12.3", [common, kitsunebi])

    assert calls[-2][0].endswith("/bin/python")
    assert calls[-2][1:] == [
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
    monkeypatch.setattr("atlas.runtime._run_stdout", lambda cmd, env=None: str(tmp_path / "pyenv/versions/3.12.3"))
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

    def fake_run_stdout(cmd: list[str], env: dict[str, str] | None = None) -> str:
        raise ValueError(f"{cmd[0]} command failed: {' '.join(cmd)}")

    monkeypatch.setattr("atlas.runtime._run_stdout", fake_run_stdout)

    status = runtime_status(tmp_path / "runtime", "3.12.3")

    assert status.provider_available is True
    assert status.pyenv_python is None
    assert status.pyenv_python_error == "pyenv command failed: pyenv prefix 3.12.3"
