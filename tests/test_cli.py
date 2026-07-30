from __future__ import annotations

import argparse
from pathlib import Path
import pwd
import runpy
import sys

import pytest

import atlas.cli as cli
from atlas.errors import LockUnavailableError
from atlas.releases import install_release
from atlas.runtime import RuntimeStatus


def _write_config(paths, releases: str = "") -> None:
    release_section = f"releases:\n{releases}" if releases else "releases: {}\n"
    (paths.etc / "config.yml").write_text(
        "runtime:\n"
        "  python:\n"
        "    version: '3.12.3'\n"
        f"{release_section}",
        encoding="utf-8",
    )


def test_release_command_and_status_flow(
    atlas_paths,
    release_factory,
    capsys,
) -> None:
    source = release_factory(
        name="sample",
        commands=("sample-show",),
        jobs=("collect",),
        service="collect",
    )

    assert cli.main(["release", "install", str(source)]) == 0
    install_output = capsys.readouterr().out
    assert "installed release: sample 1.0.0" in install_output
    assert "commands: 1" in install_output

    assert cli.main(["release", "list"]) == 0
    assert capsys.readouterr().out == "sample\n"
    assert cli.main(["release", "list", "--verbose"]) == 0
    verbose = capsys.readouterr().out
    assert "sample\t1.0.0" in verbose
    assert "commands=1\tjobs=1\tservices=1" in verbose

    assert cli.main(["command", "list"]) == 0
    assert capsys.readouterr().out == "sample-show\n"
    assert cli.main(["command", "list", "--verbose"]) == 0
    assert "sample-show\tsample\t1.0.0" in capsys.readouterr().out

    assert cli.main(["which", "sample-show"]) == 0
    assert capsys.readouterr().out.strip().endswith("/commands/sample-show.py")
    assert cli.main(["release", "shims"]) == 0
    assert "generated shims: 1" in capsys.readouterr().out
    assert (atlas_paths.shims / "sample-show").is_symlink()

    assert cli.main(["status"]) == 0
    status = capsys.readouterr().out
    assert "host name: test-host" in status
    assert f"current root: {atlas_paths.current_root}" in status
    assert "active releases count: 1" in status
    assert "commands count: 1" in status
    assert "jobs count: 1" in status
    assert "services count: 1" in status


def test_cli_run_executes_command(atlas_paths, release_factory, capfd) -> None:
    source = release_factory(name="sample", commands=("sample-show",))
    assert cli.main(["release", "install", str(source)]) == 0
    capfd.readouterr()
    assert cli.main(["run", "sample-show"]) == 0
    stdout, stderr = capfd.readouterr()
    assert "sample-show:test-host" in stdout
    assert "$ sample-show" in stderr


def test_runtime_status_with_and_without_config(
    atlas_paths,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    statuses: list[str | None] = []

    def fake_status(runtime_root, configured):
        statuses.append(configured)
        return RuntimeStatus(
            provider="pyenv",
            provider_available=True,
            configured_version=configured,
            pyenv_python=Path("/pyenv/python") if configured else None,
            artifacts_venv=atlas_paths.runtime / "python/envs/scripts",
            runtime_python=atlas_paths.runtime_python,
            runtime_python_exists=True,
        )

    monkeypatch.setattr(cli, "runtime_status", fake_status)
    assert cli.main(["runtime", "status"]) == 0
    output = capsys.readouterr().out
    assert "provider available: true" in output
    assert "artifacts venv:" in output
    assert statuses == [None]

    _write_config(atlas_paths)
    assert cli.main(["runtime", "status"]) == 0
    output = capsys.readouterr().out
    assert "configured version: 3.12.3" in output
    assert "pyenv python: /pyenv/python" in output
    assert statuses[-1] == "3.12.3"


def test_runtime_status_prints_provider_error(
    atlas_paths,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        cli,
        "runtime_status",
        lambda runtime, configured: RuntimeStatus(
            provider="pyenv",
            provider_available=False,
            configured_version=None,
            pyenv_python_error="broken",
            artifacts_venv=atlas_paths.runtime / "python/envs/scripts",
            runtime_python=atlas_paths.runtime_python,
            runtime_python_exists=False,
        ),
    )
    assert cli.main(["runtime", "status"]) == 0
    output = capsys.readouterr().out
    assert "pyenv python error: broken" in output
    assert "runtime python exists: false" in output


def test_runtime_install_uses_active_release_roots(
    atlas_paths,
    release_factory,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    source = release_factory(name="sample")
    target = install_release(source, atlas_paths.releases_root, atlas_paths.current_root)
    _write_config(atlas_paths)
    calls: list[tuple[object, ...]] = []

    def fake_install(runtime, version, roots, **kwargs):
        calls.append((runtime, version, roots, kwargs))
        return atlas_paths.runtime_python

    monkeypatch.setattr(cli, "install_runtime", fake_install)
    assert cli.main(["runtime", "install"]) == 0
    assert calls[0][0:3] == (atlas_paths.runtime, "3.12.3", [target])
    assert calls[0][3]["tmp_dir"] == atlas_paths.tmp
    assert "installed runtime python:" in capsys.readouterr().out


def test_release_update_respects_enabled_and_specific_releases(
    atlas_paths,
    release_factory,
    capsys,
) -> None:
    first = release_factory(name="first", version="1.0.0", commands=("first-show",))
    second = release_factory(name="second", version="1.0.0", commands=("second-show",))
    _write_config(
        atlas_paths,
        f"  first:\n    source: {first}\n"
        f"  second:\n    source: {second}\n    enabled: false\n",
    )

    assert cli.main(["release", "update"]) == 0
    assert (atlas_paths.current_root / "first").is_symlink()
    assert not (atlas_paths.current_root / "second").exists()
    assert cli.main(["release", "update", "second"]) == 0
    assert (atlas_paths.current_root / "second").is_symlink()
    capsys.readouterr()


def test_release_update_rolls_back_all_links_on_collision(
    atlas_paths,
    release_factory,
) -> None:
    old_first = release_factory(name="first", version="0.1.0", commands=("first-show",))
    old_second = release_factory(name="second", version="0.1.0", commands=("second-show",))
    install_release(old_first, atlas_paths.releases_root, atlas_paths.current_root)
    install_release(old_second, atlas_paths.releases_root, atlas_paths.current_root)
    old_targets = {
        name: (atlas_paths.current_root / name).resolve()
        for name in ("first", "second")
    }
    new_first = release_factory(name="first", version="2.0.0", commands=("same-command",))
    new_second = release_factory(name="second", version="2.0.0", commands=("same-command",))
    _write_config(
        atlas_paths,
        f"  first:\n    source: {new_first}\n"
        f"  second:\n    source: {new_second}\n",
    )

    assert cli.main(["release", "update"]) == 2
    assert (atlas_paths.current_root / "first").resolve() == old_targets["first"]
    assert (atlas_paths.current_root / "second").resolve() == old_targets["second"]


def test_release_update_rejects_unknown_and_manifest_name_mismatch(
    atlas_paths,
    release_factory,
    capsys,
) -> None:
    source = release_factory(name="actual")
    _write_config(atlas_paths, f"  configured:\n    source: {source}\n")
    assert cli.main(["release", "update", "missing"]) == 2
    assert "release is not configured" in capsys.readouterr().err
    assert cli.main(["release", "update", "configured"]) == 2
    assert "configured release name mismatch" in capsys.readouterr().err


def test_release_install_rolls_back_new_link_on_collision(
    atlas_paths,
    release_factory,
) -> None:
    first = release_factory(name="first", commands=("same-command",))
    second = release_factory(name="second", commands=("same-command",))
    assert cli.main(["release", "install", str(first)]) == 0
    assert cli.main(["release", "install", str(second)]) == 2
    assert (atlas_paths.current_root / "first").is_symlink()
    assert not (atlas_paths.current_root / "second").exists()


def test_job_cli_commands(
    atlas_paths,
    release_factory,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    source = release_factory(name="worker", commands=(), jobs=("collect",), timeout=20)
    install_release(source, atlas_paths.releases_root, atlas_paths.current_root)
    assert cli.main(["job", "list"]) == 0
    assert capsys.readouterr().out == "worker\tcollect\n"
    assert cli.main(["job", "list", "worker"]) == 0
    capsys.readouterr()
    assert cli.main(["job", "inspect", "worker", "collect"]) == 0
    inspect = capsys.readouterr().out
    assert "default_timeout_seconds: 20" in inspect

    calls: list[tuple[str, str, list[str]]] = []
    monkeypatch.setattr(
        cli,
        "run_job",
        lambda paths, release, job, args: calls.append((release, job, args)) or 9,
    )
    assert cli.main(["job", "run", "worker", "collect", "--", "--site", "default"]) == 9
    assert calls == [("worker", "collect", ["--site", "default"])]


def test_job_instance_cli_commands(
    atlas_paths,
    release_factory,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    source = release_factory(name="worker", commands=(), jobs=("collect",))
    install_release(source, atlas_paths.releases_root, atlas_paths.current_root)
    workdir = atlas_paths.var / "work"
    workdir.mkdir(parents=True)
    atlas_paths.jobs_dir.mkdir()
    user = pwd.getpwuid(__import__("os").geteuid()).pw_name
    (atlas_paths.jobs_dir / "sample-instance.yml").write_text(
        "schema: atlas.job-instance/v1\n"
        "release: worker\n"
        "job: collect\n"
        f"user: {user}\n"
        f"working_directory: {workdir}\n",
        encoding="utf-8",
    )
    assert cli.main(["job", "instance", "list"]) == 0
    assert capsys.readouterr().out == "sample-instance\n"
    assert cli.main(["job", "instance", "inspect", "sample-instance"]) == 0
    inspect = capsys.readouterr().out
    assert "schema: atlas.job-instance/v1" in inspect
    assert "lock: sample-instance" in inspect
    monkeypatch.setattr(cli, "run_job_instance", lambda paths, name: 11)
    assert cli.main(["job", "instance", "run", "sample-instance"]) == 11

    (atlas_paths.jobs_dir / "sample-instance.yml").write_text(
        "schema: atlas.job-instance/v1\n"
        "release: worker\n"
        "job: missing\n"
        f"user: {user}\n"
        f"working_directory: {workdir}\n",
        encoding="utf-8",
    )
    assert cli.main(["job", "instance", "list"]) == 2
    assert "unknown job" in capsys.readouterr().err
    assert cli.main(["job", "instance", "inspect", "sample-instance"]) == 2


def test_init_cli_uses_systemd_adapter(
    atlas_paths,
    release_factory,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    source = release_factory(name="worker", commands=(), jobs=("refresh",), service="refresh")
    other = release_factory(name="other", commands=("other-show",))
    install_release(source, atlas_paths.releases_root, atlas_paths.current_root)
    install_release(other, atlas_paths.releases_root, atlas_paths.current_root)
    calls: list[str] = []

    class Adapter:
        def __init__(self, **kwargs):
            assert kwargs == {"jobs_dir": atlas_paths.jobs_dir}

        def diff(self, service):
            calls.append(f"diff:{service.service.name}")
            return "unit diff\n"

        def install(self, service):
            calls.append(f"install:{service.service.name}")
            return [Path("/etc/systemd/system/atlas-worker-refresh.service")]

        def remove(self, service):
            calls.append(f"remove:{service.service.name}")
            return [Path("/etc/systemd/system/atlas-worker-refresh.service")]

    monkeypatch.setattr(cli, "SystemdAdapter", Adapter)
    assert cli.main(["init", "list"]) == 0
    assert capsys.readouterr().out == "worker\trefresh\tsystemd\n"
    assert cli.main(["init", "list", "worker"]) == 0
    capsys.readouterr()
    assert cli.main(["init", "diff", "worker", "refresh"]) == 0
    assert capsys.readouterr().out == "unit diff\n"
    assert cli.main(["init", "install", "worker", "refresh"]) == 0
    assert "atlas-worker-refresh.service" in capsys.readouterr().out
    assert cli.main(["init", "remove", "worker", "refresh"]) == 0
    capsys.readouterr()
    assert calls == ["diff:refresh", "install:refresh", "remove:refresh"]
    assert cli.main(["init", "list", "missing"]) == 2


def test_main_renders_expected_errors_and_lock_exit(
    atlas_paths,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    assert cli.main(["which", "missing"]) == 2
    assert "atlas: unknown command" in capsys.readouterr().err

    def locked(paths, name):
        raise LockUnavailableError("busy")

    monkeypatch.setattr(cli, "run_job_instance", locked)
    assert cli.main(["job", "instance", "run", "anything"]) == 75
    assert "atlas: busy" in capsys.readouterr().err


def test_status_tolerates_invalid_host(atlas_paths, capsys) -> None:
    (atlas_paths.etc / "host.yml").write_text("name: ''\n", encoding="utf-8")
    assert cli.main(["status"]) == 0
    assert "host name: unknown" in capsys.readouterr().out
    (atlas_paths.etc / "host.yml").unlink()
    assert cli.main(["status"]) == 0
    assert "host name: unknown" in capsys.readouterr().out


def test_current_snapshot_validation_and_restore(tmp_path: Path) -> None:
    current = tmp_path / "current"
    current.mkdir()
    assert cli._capture_current_targets(current, ["missing"]) == {"missing": None}
    regular = current / "regular"
    regular.write_text("bad", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a symlink"):
        cli._capture_current_targets(current, ["regular"])
    regular.unlink()
    regular.symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="target not found"):
        cli._capture_current_targets(current, ["regular"])

    target = tmp_path / "target"
    target.mkdir()
    regular.unlink()
    regular.symlink_to(target, target_is_directory=True)
    snapshots = cli._capture_current_targets(current, ["regular"])
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    regular.unlink()
    regular.symlink_to(replacement, target_is_directory=True)
    cli._restore_current_targets(current, snapshots)
    assert regular.resolve() == target
    cli._restore_current_targets(current, {"regular": None})
    assert not regular.exists()


def test_cli_module_entrypoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["atlas", "--help"])
    monkeypatch.delitem(sys.modules, "atlas.cli", raising=False)
    with pytest.raises(SystemExit) as error:
        runpy.run_module("atlas.cli", run_name="__main__")
    assert error.value.code == 0


def test_build_parser_returns_argparse_parser() -> None:
    parser = cli.build_parser()
    assert isinstance(parser, argparse.ArgumentParser)
