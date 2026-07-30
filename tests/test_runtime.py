from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess

import pytest

from atlas.runtime import (
    _executable_shebang,
    _normalize_roots,
    _runtime_requirements,
    _validate_console_script_shebangs,
    install_runtime,
    python_bin,
    runtime_status,
)


def _fake_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[list[list[str]], Path]:
    calls: list[list[str]] = []
    pyenv_root = tmp_path / "pyenv/versions/3.12.3"
    pyenv_python = pyenv_root / "bin/python"
    pyenv_python.parent.mkdir(parents=True)
    pyenv_python.write_text("", encoding="utf-8")
    monkeypatch.setattr("atlas.runtime.shutil.which", lambda _: "/usr/bin/pyenv")

    def fake_run(
        command: list[str],
        check: bool,
        capture_output: bool = False,
        text: bool = False,
        env: dict[str, str] | None = None,
    ):
        assert check is True
        calls.append(command)
        if capture_output:
            class Process:
                stdout = f"{pyenv_root}\n"

            return Process()
        if command[:3] == [str(pyenv_python), "-m", "venv"]:
            runtime_python = Path(command[3]) / "bin/python"
            runtime_python.parent.mkdir(parents=True)
            runtime_python.write_text("", encoding="utf-8")
        return None

    monkeypatch.setattr("atlas.runtime.subprocess.run", fake_run)
    return calls, pyenv_python


def test_runtime_install_builds_final_environment_and_requirements(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls, pyenv_python = _fake_runtime(monkeypatch, tmp_path)
    runtime = tmp_path / "runtime"
    old = runtime / "python/envs/scripts"
    old.mkdir(parents=True)
    (old / "stale").write_text("old", encoding="utf-8")
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "requirements.lock").write_text("one\n", encoding="utf-8")
    (first / "requirements.txt").write_text("ignored\n", encoding="utf-8")
    (second / "requirements.txt").write_text("two\n", encoding="utf-8")

    result = install_runtime(runtime, "3.12.3", [first, second])

    assert result == runtime / "python/envs/scripts/bin/python"
    assert not (result.parent.parent / "stale").exists()
    assert calls[:3] == [
        ["pyenv", "install", "-s", "3.12.3"],
        ["pyenv", "prefix", "3.12.3"],
        [str(pyenv_python), "-m", "venv", str(runtime / f"python/envs/scripts.tmp.{os.getpid()}")],
    ]
    assert calls[3][1:] == ["-m", "pip", "install", "--upgrade", "pip"]
    assert calls[4][1:] == [
        "-m",
        "pip",
        "install",
        "PyYAML",
        "-r",
        str(first / "requirements.lock"),
        "-r",
        str(second / "requirements.txt"),
    ]
    assert calls[5][1:] == ["-m", "pip", "check"]
    assert not list((runtime / "python/envs").glob("scripts.bak.*"))


def test_runtime_install_sets_default_and_explicit_temp_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    envs: list[dict[str, str]] = []
    calls, _ = _fake_runtime(monkeypatch, tmp_path)
    original = subprocess.run

    def capture(
        command,
        check,
        capture_output=False,
        text=False,
        env=None,
    ):
        assert env is not None
        envs.append(env)
        return original(command, check=check, capture_output=capture_output, text=text, env=env)

    fake_run = __import__("atlas.runtime").runtime.subprocess.run

    def wrapped(command, check, capture_output=False, text=False, env=None):
        assert env is not None
        envs.append(env)
        return fake_run(
            command,
            check=check,
            capture_output=capture_output,
            text=text,
            env=env,
        )

    monkeypatch.setattr("atlas.runtime.subprocess.run", wrapped)
    monkeypatch.delenv("TMPDIR", raising=False)
    monkeypatch.delenv("PYTHON_BUILD_CACHE_PATH", raising=False)
    runtime = tmp_path / "opt/atlas/runtime"
    tmp_dir = tmp_path / "opt/atlas/tmp"
    cache = tmp_path / "var/cache/python-build"
    install_runtime(runtime, "3.12.3", tmp_dir=tmp_dir, python_build_cache_path=cache)
    assert envs and all(env["TMPDIR"] == str(tmp_dir) for env in envs)
    assert all(env["PYTHON_BUILD_CACHE_PATH"] == str(cache) for env in envs)
    assert tmp_dir.is_dir() and cache.is_dir()
    assert calls

    explicit = tmp_path / "explicit"
    monkeypatch.setenv("TMPDIR", str(explicit))
    install_runtime(tmp_path / "second-runtime", "3.12.3", tmp_dir=tmp_path / "unused")
    assert explicit.is_dir()
    assert not (tmp_path / "unused").exists()


def test_runtime_install_requires_pyenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("atlas.runtime.shutil.which", lambda _: None)
    with pytest.raises(ValueError, match="pyenv command is required"):
        install_runtime(tmp_path / "runtime", "3.12.3")


def test_runtime_subprocess_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("atlas.runtime.shutil.which", lambda _: "/usr/bin/pyenv")

    def missing(*args, **kwargs):
        raise FileNotFoundError("missing")

    monkeypatch.setattr("atlas.runtime.subprocess.run", missing)
    with pytest.raises(ValueError, match="pyenv command is required"):
        install_runtime(tmp_path / "runtime", "3.12.3")

    def failed(command, **kwargs):
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr("atlas.runtime.subprocess.run", failed)
    with pytest.raises(ValueError, match="pyenv command failed"):
        install_runtime(tmp_path / "runtime", "3.12.3")


def test_runtime_rejects_empty_prefix_and_missing_python(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("atlas.runtime.shutil.which", lambda _: "/usr/bin/pyenv")

    def empty(command, **kwargs):
        class Process:
            stdout = "\n"

        return Process()

    monkeypatch.setattr("atlas.runtime.subprocess.run", empty)
    with pytest.raises(ValueError, match="did not return an install prefix"):
        install_runtime(tmp_path / "runtime", "3.12.3")

    def missing_python(command, **kwargs):
        class Process:
            stdout = str(tmp_path / "missing-prefix")

        return Process()

    monkeypatch.setattr("atlas.runtime.subprocess.run", missing_python)
    with pytest.raises(ValueError, match="Python executable not found"):
        install_runtime(tmp_path / "runtime", "3.12.3")


def test_runtime_install_rolls_back_after_pip_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls, _ = _fake_runtime(monkeypatch, tmp_path)
    fake_run = __import__("atlas.runtime").runtime.subprocess.run

    def fail_pip(command, **kwargs):
        if command[1:] == ["-m", "pip", "install", "--upgrade", "pip"]:
            raise subprocess.CalledProcessError(1, command)
        return fake_run(command, **kwargs)

    monkeypatch.setattr("atlas.runtime.subprocess.run", fail_pip)
    runtime = tmp_path / "runtime"
    old = runtime / "python/envs/scripts"
    old.mkdir(parents=True)
    (old / "old").write_text("old", encoding="utf-8")
    with pytest.raises(ValueError, match="command failed"):
        install_runtime(runtime, "3.12.3")
    assert (old / "old").exists()
    assert calls


@pytest.mark.parametrize("has_existing", [False, True])
def test_runtime_install_handles_final_rename_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    has_existing: bool,
) -> None:
    _fake_runtime(monkeypatch, tmp_path)
    runtime = tmp_path / "runtime"
    environment = runtime / "python/envs/scripts"
    if has_existing:
        environment.mkdir(parents=True)
        (environment / "old").write_text("old", encoding="utf-8")
    original = Path.rename

    def fail_temporary(self: Path, target: Path):
        if self.name.startswith("scripts.tmp."):
            raise RuntimeError("rename failed")
        return original(self, target)

    monkeypatch.setattr(Path, "rename", fail_temporary)
    with pytest.raises(RuntimeError, match="rename failed"):
        install_runtime(runtime, "3.12.3")
    assert environment.exists() is has_existing
    if has_existing:
        assert (environment / "old").exists()


def test_console_script_shebang_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls, pyenv_python = _fake_runtime(monkeypatch, tmp_path)
    fake_run = __import__("atlas.runtime").runtime.subprocess.run

    def add_stale_script(command, **kwargs):
        result = fake_run(command, **kwargs)
        if command[:3] == [str(pyenv_python), "-m", "venv"]:
            script = Path(command[3]) / "bin/tool"
            script.write_text(f"#!{command[3]}/bin/python\n", encoding="utf-8")
            script.chmod(script.stat().st_mode | stat.S_IXUSR)
        return result

    monkeypatch.setattr("atlas.runtime.subprocess.run", add_stale_script)
    with pytest.raises(ValueError, match="shebang must point"):
        install_runtime(tmp_path / "runtime", "3.12.3")
    assert calls


def test_console_script_rejects_other_runtime_environment(tmp_path: Path) -> None:
    environment = tmp_path / "envs/scripts"
    bin_dir = environment / "bin"
    bin_dir.mkdir(parents=True)
    tool = bin_dir / "tool"
    tool.write_text(f"#!{tmp_path / 'envs/other/bin/python'}\n", encoding="utf-8")
    tool.chmod(0o755)
    with pytest.raises(ValueError, match="shebang must point"):
        _validate_console_script_shebangs(environment, tmp_path / "unrelated")

    tool.write_text(f"#!{environment / 'bin/python'}\n", encoding="utf-8")
    _validate_console_script_shebangs(environment, tmp_path / "unrelated")


def test_executable_shebang_helper(tmp_path: Path) -> None:
    assert _executable_shebang(tmp_path / "missing") is None
    plain = tmp_path / "plain"
    plain.write_text("#!/bin/sh\n", encoding="utf-8")
    assert _executable_shebang(plain) is None
    plain.chmod(0o755)
    plain.write_text("echo x\n", encoding="utf-8")
    assert _executable_shebang(plain) is None
    plain.write_text("#!/bin/sh\r\n", encoding="utf-8")
    assert _executable_shebang(plain) == "#!/bin/sh"


def test_runtime_status_variants(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    monkeypatch.setattr("atlas.runtime.shutil.which", lambda _: None)
    unavailable = runtime_status(runtime, "3.12.3")
    assert unavailable.provider_available is False
    assert unavailable.pyenv_python is None
    assert unavailable.runtime_python_exists is False

    prefix = tmp_path / "pyenv/versions/3.12.3"

    def success(command, **kwargs):
        class Process:
            stdout = f"{prefix}\n"

        return Process()

    monkeypatch.setattr("atlas.runtime.shutil.which", lambda _: "/usr/bin/pyenv")
    monkeypatch.setattr("atlas.runtime.subprocess.run", success)
    available = runtime_status(runtime, "3.12.3")
    assert available.pyenv_python == prefix / "bin/python"
    assert available.artifacts_venv == runtime / "python/envs/scripts"

    def failure(command, **kwargs):
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr("atlas.runtime.subprocess.run", failure)
    failed = runtime_status(runtime, "3.12.3")
    assert "pyenv command failed" in str(failed.pyenv_python_error)

    def missing(command, **kwargs):
        raise FileNotFoundError("missing")

    monkeypatch.setattr("atlas.runtime.subprocess.run", missing)
    missing_status = runtime_status(runtime, "3.12.3")
    assert "command is required" in str(missing_status.pyenv_python_error)
    assert runtime_status(runtime).configured_version is None


def test_runtime_small_helpers(tmp_path: Path) -> None:
    root = tmp_path / "venv"
    assert python_bin(root) == root / "bin/python"
    assert _normalize_roots(None) is None
    assert _normalize_roots(root) == [root]
    assert _normalize_roots((root,)) == [root]
    assert _runtime_requirements(None) == ["PyYAML"]
    root.mkdir()
    assert _runtime_requirements([root]) == ["PyYAML"]
