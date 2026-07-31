from __future__ import annotations

import io
import runpy
import subprocess
import sys
import tarfile
import warnings
import zipfile
from pathlib import Path

import pytest

from atlas import cli
from atlas.commands import command_name_from_relative_path, discover_commands
from atlas.config import AtlasConfig, RuntimeConfig, ScriptsConfig
from atlas.launchers import regenerate_shims
from atlas.releases import (
    install_named_release,
    install_release,
    read_version,
    validate_release,
)
from atlas.runner import resolve_command_path
from atlas.runtime import RuntimeStatus, install_runtime
from atlas.scriptsets import active_releases, validate_release_name
from atlas.sources import (
    clone_git_source,
    download_archive,
    extract_archive,
    resolve_source,
)
from atlas.yamlutil import dump_yaml_file, load_yaml_file


def _set_env(monkeypatch: pytest.MonkeyPatch, home: Path, etc: Path, var: Path) -> None:
    monkeypatch.setenv("ATLAS_HOME", str(home))
    monkeypatch.setenv("ATLAS_ETC_DIR", str(etc))
    monkeypatch.setenv("ATLAS_VAR_DIR", str(var))
    monkeypatch.setenv("ATLAS_RUNTIME_DIR", str(home / "runtime"))
    monkeypatch.setenv("ATLAS_SCRIPTS_CURRENT_DIR", str(home / "scripts/current"))
    monkeypatch.setenv("ATLAS_SCRIPTS_DIR", str(home / "scripts/current"))


def _release(path: Path, *, command_name: str = "sample", version: str = "0.1.0") -> Path:
    (path / "commands").mkdir(parents=True, exist_ok=True)
    (path / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    (path / "commands" / f"{command_name}.py").write_text("print('ok')\n", encoding="utf-8")
    return path


def _fake_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[list[str]]:
    calls: list[list[str]] = []
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
    return calls


def test_cli_status_handles_invalid_host_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True)
    (etc / "host.yml").write_text("[]\n", encoding="utf-8")
    _set_env(monkeypatch, home, etc, var)

    assert cli.main(["status"]) == 0

    assert "host name: unknown" in capsys.readouterr().out


def test_cli_status_without_host_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True)
    _set_env(monkeypatch, home, etc, var)

    assert cli.main(["status"]) == 0

    assert "host name: unknown" in capsys.readouterr().out


def test_cli_runtime_status_without_config_and_with_pyenv_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True)
    _set_env(monkeypatch, home, etc, var)
    monkeypatch.setattr(
        cli,
        "runtime_status",
        lambda runtime_root, python_version: RuntimeStatus(
            provider="pyenv",
            provider_available=True,
            scripts_venv=home / "runtime/python/envs/scripts",
            scripts_python=home / "runtime/python/envs/scripts/bin/python",
            scripts_python_exists=False,
            pyenv_python_error="pyenv command failed",
        ),
    )

    assert cli.main(["runtime", "status"]) == 0

    out = capsys.readouterr().out
    assert "provider available: true" in out
    assert "pyenv python error: pyenv command failed" in out
    assert "scripts python exists: false" in out


def test_cli_runtime_status_without_pyenv_path_or_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True)
    _set_env(monkeypatch, home, etc, var)
    monkeypatch.setattr(
        cli,
        "runtime_status",
        lambda runtime_root, python_version: RuntimeStatus(
            provider="pyenv",
            provider_available=False,
            scripts_venv=home / "runtime/python/envs/scripts",
            scripts_python=home / "runtime/python/envs/scripts/bin/python",
            scripts_python_exists=False,
        ),
    )

    assert cli.main(["runtime", "status"]) == 0

    out = capsys.readouterr().out
    assert "pyenv python:" not in out
    assert "pyenv python error:" not in out
    assert "scripts venv:" in out


def test_cli_runtime_install_prints_result(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True)
    (etc / "config.yml").write_text(
        "runtime:\n  python:\n    version: '3.12.3'\nscripts:\n  source: sample\n", encoding="utf-8"
    )
    _set_env(monkeypatch, home, etc, var)
    monkeypatch.setattr(
        cli,
        "install_runtime",
        lambda runtime, version, roots, tmp_dir=None, python_build_cache_path=None: home / "runtime/bin/python",
    )

    assert cli.main(["runtime", "install"]) == 0

    out = capsys.readouterr().out
    assert f"installed scripts python: {home / 'runtime/bin/python'}" in out
    assert "configured python version: 3.12.3" in out


def test_cli_scripts_update_rejects_unconfigured_release(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True)
    (etc / "config.yml").write_text(
        "runtime:\n  python:\n    version: '3.12.3'\nscripts:\n  releases:\n    known: sample\n",
        encoding="utf-8",
    )
    _set_env(monkeypatch, home, etc, var)

    with pytest.raises(ValueError, match="scripts release is not configured: missing"):
        cli.main(["scripts", "update", "missing"])


def test_cli_scripts_shims_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True)
    _set_env(monkeypatch, home, etc, var)
    release = _release(tmp_path / "release")
    install_named_release(release, home / "scripts/releases", home / "scripts/current", "default")

    assert cli.main(["scripts", "shims"]) == 0

    assert "generated shims: 1" in capsys.readouterr().out


def test_cli_current_target_snapshot_rejects_broken_state(tmp_path: Path) -> None:
    current = tmp_path / "current"
    current.mkdir()
    (current / "default").mkdir()

    with pytest.raises(ValueError, match="scripts current entry must be a symlink"):
        cli._capture_current_targets(current, ["default"])

    (current / "default").rmdir()
    (current / "default").symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="active release target not found"):
        cli._capture_current_targets(current, ["default"])


def test_cli_module_entrypoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["atlas", "--help"])
    monkeypatch.delitem(sys.modules, "atlas.cli", raising=False)
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("atlas.cli", run_name="__main__")
    assert exc.value.code == 0


def test_command_validation_rejects_double_dash_and_trailing_dash() -> None:
    with pytest.raises(ValueError, match="invalid command name: foo--bar"):
        command_name_from_relative_path(Path("foo-") / "bar.py")
    with pytest.raises(ValueError, match="invalid command name: foo-"):
        command_name_from_relative_path(Path("foo-.py"))


def test_discover_commands_rejects_missing_symlink_and_traversal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="commands directory not found"):
        discover_commands(tmp_path / "missing")

    commands = tmp_path / "commands"
    commands.mkdir()
    target = tmp_path / "target.py"
    target.write_text("print('x')\n", encoding="utf-8")
    (commands / "linked.py").symlink_to(target)
    with pytest.raises(ValueError, match="symlink is not allowed"):
        discover_commands(commands)

    (commands / "linked.py").unlink()
    inner = commands / "sample.py"
    inner.write_text("print('x')\n", encoding="utf-8")
    original_resolve = Path.resolve

    def fake_resolve(self: Path, *args, **kwargs):
        if self == inner:
            return tmp_path / "outside.py"
        return original_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fake_resolve)
    with pytest.raises(ValueError, match="path traversal detected"):
        discover_commands(commands)


def test_release_validation_rejects_missing_empty_and_invalid_inputs(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="missing VERSION file"):
        read_version(tmp_path / "missing-release")

    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "VERSION").write_text("\n", encoding="utf-8")
    with pytest.raises(ValueError, match="VERSION is empty"):
        read_version(empty)

    with pytest.raises(ValueError, match="source directory not found"):
        validate_release(tmp_path / "missing")

    for name in ["current", "Bad"]:
        with pytest.raises(ValueError, match="invalid release name"):
            validate_release_name(name)

    release = _release(tmp_path / "release")
    for name in ["current", "Bad"]:
        with pytest.raises(ValueError, match="invalid release name"):
            install_named_release(release, tmp_path / "releases", tmp_path / "current", name)


def test_install_release_rolls_back_when_replacement_rename_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = _release(tmp_path / "source", version="0.1.0")
    releases = tmp_path / "releases"
    current = tmp_path / "current"
    install_release(source, releases, current)
    target = releases / "0.1.0"
    original_rename = Path.rename

    def fail_staging_rename(self: Path, target_path: Path):
        if ".tmp." in self.name:
            raise RuntimeError("rename failed")
        return original_rename(self, target_path)

    monkeypatch.setattr(Path, "rename", fail_staging_rename)
    with pytest.raises(RuntimeError, match="rename failed"):
        install_release(source, releases, current)

    assert current.resolve() == target
    assert target.exists()


def test_install_release_cleans_staging_when_initial_rename_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = _release(tmp_path / "source", version="0.1.0")
    releases = tmp_path / "releases"
    current = tmp_path / "current"
    original_rename = Path.rename

    def fail_staging_rename(self: Path, target_path: Path):
        if ".tmp." in self.name:
            raise RuntimeError("rename failed")
        return original_rename(self, target_path)

    monkeypatch.setattr(Path, "rename", fail_staging_rename)
    with pytest.raises(RuntimeError, match="rename failed"):
        install_release(source, releases, current)

    assert not (releases / "0.1.0").exists()


def test_install_named_release_backs_up_legacy_current_file(tmp_path: Path) -> None:
    source = _release(tmp_path / "source")
    current = tmp_path / "scripts/current"
    current.parent.mkdir(parents=True)
    current.write_text("legacy", encoding="utf-8")

    install_named_release(source, tmp_path / "scripts/releases", current, "default")

    assert (current / "default").is_symlink()
    assert len(list(current.parent.glob("current.legacy.*"))) == 1


def test_runner_resolve_unknown_command(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown command: missing"):
        resolve_command_path(tmp_path / "current", "missing")


def test_runner_redacts_sensitive_arguments(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True)
    var.mkdir(parents=True)
    _set_env(monkeypatch, home, etc, var)
    scripts_python = home / "runtime/python/envs/scripts/bin/python"
    scripts_python.parent.mkdir(parents=True)
    scripts_python.symlink_to(Path("/usr/bin/python3"))
    release = _release(tmp_path / "release")
    install_named_release(release, home / "scripts/releases", home / "scripts/current", "default")
    other = _release(tmp_path / "other", command_name="other")
    install_named_release(other, home / "scripts/releases", home / "scripts/current", "other")
    class Proc:
        returncode = 0

    monkeypatch.setattr("atlas.runner.subprocess.run", lambda *args, **kwargs: Proc())

    assert cli.main(["run", "sample", "--token", "abc", "--api-key=def", "DB_PASSWORD=ghi"]) == 0

    log = (var / "logs/runs.jsonl").read_text(encoding="utf-8")
    assert '"args": ["--token", "***", "--api-key=***", "DB_PASSWORD=***"]' in log


def test_runtime_install_uses_no_extra_requirements_for_release_without_requirements(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _fake_runtime(monkeypatch, tmp_path)
    scripts_root = tmp_path / "scripts/current/default"
    scripts_root.mkdir(parents=True)

    install_runtime(tmp_path / "runtime", "3.12.3", [scripts_root])

    assert calls[-2][1:] == ["-m", "pip", "install", "fire", "PyYAML"]


def test_runtime_helpers_report_subprocess_failures(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def missing_run(*args, **kwargs):
        raise FileNotFoundError("missing")

    monkeypatch.setattr("atlas.runtime.subprocess.run", missing_run)
    monkeypatch.setattr("atlas.runtime.shutil.which", lambda _: "/usr/bin/pyenv")
    with pytest.raises(ValueError, match="pyenv command is required"):
        install_runtime(tmp_path / "runtime", "3.12.3")

    def failing_run(cmd, check, capture_output=False, text=False, env=None):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr("atlas.runtime.subprocess.run", failing_run)
    with pytest.raises(ValueError, match="pyenv command failed"):
        install_runtime(tmp_path / "runtime", "3.12.3")


def test_runtime_status_reports_pyenv_prefix_failures(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("atlas.runtime.shutil.which", lambda _: "/usr/bin/pyenv")

    def missing_pyenv(cmd, check, capture_output=False, text=False, env=None):
        raise FileNotFoundError("pyenv")

    monkeypatch.setattr("atlas.runtime.subprocess.run", missing_pyenv)
    status = __import__("atlas.runtime").runtime.runtime_status(tmp_path / "runtime", "3.12.3")
    assert status.pyenv_python_error == "pyenv command is required for atlas runtime install"

    def failed_pyenv(cmd, check, capture_output=False, text=False, env=None):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr("atlas.runtime.subprocess.run", failed_pyenv)
    status = __import__("atlas.runtime").runtime.runtime_status(tmp_path / "runtime", "3.12.3")
    assert status.pyenv_python_error == "pyenv command failed: pyenv prefix 3.12.3"


def test_runtime_install_rejects_empty_pyenv_prefix_and_missing_python(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("atlas.runtime.shutil.which", lambda _: "/usr/bin/pyenv")

    def empty_prefix(cmd, check, capture_output=False, text=False, env=None):
        class Proc:
            stdout = "\n"

        return Proc()

    monkeypatch.setattr("atlas.runtime.subprocess.run", empty_prefix)
    with pytest.raises(ValueError, match="did not return an install prefix"):
        install_runtime(tmp_path / "runtime", "3.12.3")

    def missing_python(cmd, check, capture_output=False, text=False, env=None):
        class Proc:
            stdout = f"{tmp_path / 'pyenv/versions/3.12.3'}\n"

        return Proc()

    monkeypatch.setattr("atlas.runtime.subprocess.run", missing_python)
    with pytest.raises(ValueError, match="pyenv Python executable not found"):
        install_runtime(tmp_path / "runtime", "3.12.3")


def test_runtime_install_rolls_back_existing_venv_when_final_rename_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_runtime(monkeypatch, tmp_path)
    runtime = tmp_path / "runtime"
    scripts = runtime / "python/envs/scripts"
    scripts.mkdir(parents=True)
    (scripts / "old.txt").write_text("old", encoding="utf-8")
    original_rename = Path.rename

    def fail_tmp_scripts_rename(self: Path, target_path: Path):
        if self.name.startswith("scripts.tmp."):
            raise RuntimeError("rename failed")
        return original_rename(self, target_path)

    monkeypatch.setattr(Path, "rename", fail_tmp_scripts_rename)
    with pytest.raises(RuntimeError, match="rename failed"):
        install_runtime(runtime, "3.12.3")

    assert (scripts / "old.txt").exists()


def test_runtime_install_leaves_no_venv_when_final_rename_fails_without_backup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_runtime(monkeypatch, tmp_path)
    runtime = tmp_path / "runtime"
    original_rename = Path.rename

    def fail_tmp_scripts_rename(self: Path, target_path: Path):
        if self.name.startswith("scripts.tmp."):
            raise RuntimeError("rename failed")
        return original_rename(self, target_path)

    monkeypatch.setattr(Path, "rename", fail_tmp_scripts_rename)
    with pytest.raises(RuntimeError, match="rename failed"):
        install_runtime(runtime, "3.12.3")

    assert not (runtime / "python/envs/scripts").exists()


def test_scriptsets_rejects_invalid_current_root_and_entries(tmp_path: Path) -> None:
    assert active_releases(tmp_path / "missing") == []

    current = tmp_path / "current"
    current.write_text("not a dir", encoding="utf-8")
    with pytest.raises(ValueError, match="scripts current root must be a directory"):
        active_releases(current)

    current.unlink()
    current.mkdir()
    (current / "regular").write_text("ignored", encoding="utf-8")
    assert active_releases(current) == []

    (current / "Bad").symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="invalid release name"):
        active_releases(current)

    (current / "Bad").unlink()
    (current / "missing").symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="active release target not found"):
        active_releases(current)


def test_regenerate_shims_rejects_command_path_directory(tmp_path: Path) -> None:
    current = tmp_path / "current"
    release = _release(tmp_path / "release")
    current.mkdir()
    (current / "default").symlink_to(release, target_is_directory=True)
    shims = tmp_path / "shims"
    (shims / "sample").mkdir(parents=True)

    with pytest.raises(ValueError, match="shim path is a directory"):
        regenerate_shims(current, shims, tmp_path / "script-runner")


def test_sources_rejects_absolute_archive_members(tmp_path: Path) -> None:
    tar_path = tmp_path / "absolute.tar"
    with tarfile.open(tar_path, "w") as tf:
        info = tarfile.TarInfo("/absolute.py")
        info.size = 0
        tf.addfile(info, io.BytesIO())
    with pytest.raises(ValueError, match="absolute path is not allowed"):
        resolve_source(str(tar_path), cache_dir=tmp_path / "cache")

    zip_path = tmp_path / "absolute.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("/absolute.py", "")
    with pytest.raises(ValueError, match="absolute path is not allowed"):
        resolve_source(str(zip_path), cache_dir=tmp_path / "cache")


def test_sources_rejects_zip_symlink(tmp_path: Path) -> None:
    zip_path = tmp_path / "link.zip"
    info = zipfile.ZipInfo("release/link.py")
    info.external_attr = 0o120777 << 16
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(info, "target")

    with pytest.raises(ValueError, match="archive link is not allowed"):
        resolve_source(str(zip_path), cache_dir=tmp_path / "cache")


def test_sources_accepts_archive_with_release_at_root(tmp_path: Path) -> None:
    release = _release(tmp_path / "release")
    archive = tmp_path / "root.tar"
    with tarfile.open(archive, "w") as tf:
        for item in release.rglob("*"):
            tf.add(item, arcname=str(item.relative_to(release)))

    resolved = resolve_source(str(archive), cache_dir=tmp_path / "cache")

    assert resolved.name.startswith("archive.tmp.")
    assert (resolved / "VERSION").exists()


def test_sources_rejects_archives_without_release_and_unsupported_archive(tmp_path: Path) -> None:
    archive = tmp_path / "bad.tar"
    with tarfile.open(archive, "w") as tf:
        info = tarfile.TarInfo("README.md")
        info.size = 0
        tf.addfile(info, io.BytesIO())
    with pytest.raises(ValueError, match="does not contain an Atlas scripts release"):
        resolve_source(str(archive), cache_dir=tmp_path / "cache")

    unsupported = tmp_path / "release.rar"
    unsupported.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported scripts source"):
        resolve_source(str(unsupported), cache_dir=tmp_path / "cache")

    with pytest.raises(ValueError, match="unsupported archive source"):
        extract_archive(unsupported, tmp_path / "cache")


def test_sources_tar_extract_falls_back_without_data_filter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    release = _release(tmp_path / "release")
    archive = tmp_path / "release.tar"
    with tarfile.open(archive, "w") as tf:
        tf.add(release, arcname="release")
    if hasattr(tarfile, "data_filter"):
        monkeypatch.delattr(tarfile, "data_filter")

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        resolved = extract_archive(archive, tmp_path / "cache")

    assert (resolved / "VERSION").exists()


def test_sources_rejects_remote_archive_without_archive_suffix(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported archive source"):
        download_archive("https://example.test/release.bin", tmp_path / "cache")


def test_sources_git_variants_and_failures(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr("atlas.sources.subprocess.run", lambda cmd, check: calls.append(cmd))
    resolved = resolve_source("git+https://example.test/scripts.git", cache_dir=tmp_path / "cache")
    assert resolved == tmp_path / "cache/sources" / f"git.tmp.{__import__('os').getpid()}"
    assert calls == [["git", "clone", "--depth", "1", "https://example.test/scripts.git", str(resolved)]]

    with pytest.raises(ValueError, match="repository URL is required"):
        clone_git_source("git+", tmp_path / "cache")

    def missing_git(cmd, check):
        raise FileNotFoundError("git")

    monkeypatch.setattr("atlas.sources.subprocess.run", missing_git)
    with pytest.raises(ValueError, match="git command is required"):
        clone_git_source("git+https://example.test/scripts.git", tmp_path / "cache")

    def failed_git(cmd, check):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr("atlas.sources.subprocess.run", failed_git)
    with pytest.raises(ValueError, match="git source clone failed"):
        clone_git_source("git+https://example.test/scripts.git", tmp_path / "cache")


def test_resolve_source_requires_cache_for_remote_sources_and_handles_missing_local(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="scripts source is required"):
        resolve_source("  ")
    with pytest.raises(ValueError, match="cache_dir is required for git"):
        resolve_source("git+https://example.test/scripts.git")
    with pytest.raises(ValueError, match="cache_dir is required for remote archive"):
        resolve_source("https://example.test/release.tar.gz")

    archive = tmp_path / "release.zip"
    archive.write_text("not zip", encoding="utf-8")
    with pytest.raises(ValueError, match="cache_dir is required for archive"):
        resolve_source(str(archive))

    assert resolve_source(str(tmp_path / "missing")) == tmp_path / "missing"
    config = AtlasConfig(
        path=tmp_path / "config.yml",
        runtime=RuntimeConfig(python_version="3.12.3"),
        scripts=ScriptsConfig(source="alias"),
    )
    assert resolve_source("alias", config=config) == Path("alias")
    assert resolve_source("missing", cache_dir=tmp_path / "cache") == Path("missing")


def test_yaml_utilities(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="file not found"):
        load_yaml_file(tmp_path / "missing.yml")

    path = tmp_path / "nested/config.yml"
    dump_yaml_file(path, {"a": 1})
    assert load_yaml_file(path) == {"a": 1}
