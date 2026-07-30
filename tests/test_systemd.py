from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from atlas import cli
from atlas.catalog import resolve_service
from atlas.init.base import InitAdapter
from atlas.init.systemd import SystemdAdapter
from atlas.releases import install_release


def _service(paths, release_factory):
    source = release_factory(
        name="worker",
        commands=(),
        jobs=("refresh",),
        service="refresh",
    )
    paths.jobs_dir.mkdir(parents=True, exist_ok=True)
    (paths.jobs_dir / "sample-instance.yml").write_text(
        "schema: atlas.job-instance/v1\n"
        "release: worker\n"
        "job: refresh\n"
        "user: ops\n"
        "working_directory: /tmp\n",
        encoding="utf-8",
    )
    target = install_release(
        source,
        paths.scripts_releases_root,
        paths.scripts_current_root,
    )
    return target, resolve_service(
        paths.scripts_current_root,
        "worker",
        "refresh",
    )


def test_systemd_diff_install_and_remove(
    atlas_paths,
    release_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, service = _service(atlas_paths, release_factory)
    destination = atlas_paths.var / "systemd"
    calls: list[list[str]] = []
    ownership: list[tuple[int, int, int]] = []
    monkeypatch.setattr(
        "atlas.init.systemd.subprocess.run",
        lambda command, check: calls.append(command),
    )
    monkeypatch.setattr(
        "atlas.init.systemd.os.fchown",
        lambda descriptor, uid, gid: ownership.append((descriptor, uid, gid)),
    )
    adapter: InitAdapter = SystemdAdapter(
        destination,
        "fake-systemctl",
        jobs_dir=atlas_paths.jobs_dir,
    )

    before = adapter.diff(service)
    assert "atlas-worker-refresh.service" in before
    assert "atlas-worker-refresh.timer" in before
    installed = adapter.install(service)

    assert installed == [
        destination / "atlas-worker-refresh.service",
        destination / "atlas-worker-refresh.timer",
    ]
    assert all(path.stat().st_mode & 0o777 == 0o644 for path in installed)
    assert ownership and all(uid == gid == 0 for _, uid, gid in ownership)
    assert calls == [["fake-systemctl", "daemon-reload"]]
    assert adapter.diff(service) == ""

    (source / "init/systemd/refresh.service").write_text(
        "[Unit]\nDescription=Changed\n"
        "[Service]\nUser=ops\n"
        "ExecStart=/opt/atlas/bin/atlas job instance run sample-instance\n",
        encoding="utf-8",
    )
    assert "Description=Changed" in adapter.diff(service)
    removed = adapter.remove(service)
    assert removed == installed
    assert calls[-1] == ["fake-systemctl", "daemon-reload"]
    assert adapter.remove(service) == []
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("filename", "content", "message"),
    [
        ("refresh.service", "bad\n", "invalid systemd unit"),
        (
            "refresh.service",
            "# [Unit]\n[Service]\n"
            "ExecStart=/opt/atlas/bin/atlas job instance run sample-instance\n",
            "invalid systemd unit",
        ),
        ("refresh.service", "[Unit]\n", r"lacks \[Service\]"),
        (
            "refresh.service",
            "[Unit]\n[Service]\nExecStart=/bin/echo bad\n",
            "stable Atlas launcher",
        ),
        (
            "refresh.service",
            "[Unit]\n[Service]\nUser=ops\n"
            "ExecStart=/tmp/untrusted/bin/atlas job instance run sample-instance\n",
            "stable Atlas launcher",
        ),
        (
            "refresh.service",
            "[Unit]\n[Service]\nUser=ops\n"
            "ExecStart=/opt/atlas/bin/atlas job run worker wrong-job\n",
            "matching job instance for worker/refresh",
        ),
        (
            "refresh.service",
            "[Unit]\n[Service]\nUser=ops\n"
            "ExecStart=/opt/atlas/bin/atlas job run worker refresh\n",
            "matching job instance for worker/refresh",
        ),
        (
            "refresh.service",
            "[Unit]\n[Service]\nUser=ops\n"
            "ExecStart=/opt/atlas/bin/atlas job run worker refresh\n"
            "ExecStart=/opt/atlas/bin/atlas job run worker refresh\n",
            "exactly one ExecStart",
        ),
        (
            "refresh.service",
            "[Unit]\n[Service]\nUser=ops\n"
            "ExecStart=/opt/atlas/bin/atlas job instance run sample-instance\n"
            "# /opt/atlas/releases/worker/1.0.0\n",
            "versioned release path",
        ),
        ("refresh.timer", "[Unit]\n", r"lacks \[Timer\]"),
        (
            "refresh.timer",
            "[Unit]\n[Timer]\nOnCalendar=hourly\nUnit=wrong.service\n",
            "must reference Unit=atlas-worker-refresh.service",
        ),
    ],
)
def test_systemd_validation_rejects_bad_units(
    atlas_paths,
    release_factory,
    filename: str,
    content: str,
    message: str,
) -> None:
    source, service = _service(atlas_paths, release_factory)
    (source / "init/systemd" / filename).write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        SystemdAdapter(
            atlas_paths.var / "systemd",
            jobs_dir=atlas_paths.jobs_dir,
        ).validate(service)


def test_systemd_rejects_symlink_destinations(
    atlas_paths,
    release_factory,
    tmp_path: Path,
) -> None:
    _, service = _service(atlas_paths, release_factory)
    destination = atlas_paths.var / "systemd"
    destination.mkdir(parents=True)
    target = tmp_path / "target"
    target.write_text("x", encoding="utf-8")
    unit = destination / "atlas-worker-refresh.service"
    unit.symlink_to(target)
    adapter = SystemdAdapter(destination, jobs_dir=atlas_paths.jobs_dir)
    with pytest.raises(ValueError, match="destination must not be a symlink"):
        adapter.diff(service)
    with pytest.raises(ValueError, match="destination must not be a symlink"):
        adapter.install(service)
    with pytest.raises(ValueError, match="destination must not be a symlink"):
        adapter.remove(service)


def test_systemd_rejects_non_file_destinations(
    atlas_paths,
    release_factory,
) -> None:
    _, service = _service(atlas_paths, release_factory)
    destination = atlas_paths.var / "systemd"
    unit = destination / "atlas-worker-refresh.service"
    unit.mkdir(parents=True)
    adapter = SystemdAdapter(destination, jobs_dir=atlas_paths.jobs_dir)
    with pytest.raises(ValueError, match="destination must be a regular file"):
        adapter.diff(service)
    with pytest.raises(ValueError, match="destination must be a regular file"):
        adapter.install(service)
    with pytest.raises(ValueError, match="destination must be a regular file"):
        adapter.remove(service)


def test_systemd_rejects_relative_and_unsafe_destination_root(
    atlas_paths,
    release_factory,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="must be absolute"):
        SystemdAdapter(Path("relative"))
    with pytest.raises(ValueError, match="jobs directory must be absolute"):
        SystemdAdapter(tmp_path / "systemd", jobs_dir=Path("relative"))
    _, service = _service(atlas_paths, release_factory)
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "systemd"
    link.symlink_to(target, target_is_directory=True)
    adapter = SystemdAdapter(link, jobs_dir=atlas_paths.jobs_dir)
    with pytest.raises(ValueError, match="root must be a directory"):
        adapter.diff(service)
    with pytest.raises(ValueError, match="root must be a directory"):
        adapter.install(service)
    with pytest.raises(ValueError, match="root must be a directory"):
        adapter.remove(service)

    dangling = tmp_path / "dangling-systemd"
    dangling.symlink_to(tmp_path / "missing", target_is_directory=True)
    with pytest.raises(ValueError, match="root must be a directory"):
        SystemdAdapter(
            dangling,
            jobs_dir=atlas_paths.jobs_dir,
        ).diff(service)

    regular = tmp_path / "regular-systemd"
    regular.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ValueError, match="root must be a directory"):
        SystemdAdapter(
            regular,
            jobs_dir=atlas_paths.jobs_dir,
        ).diff(service)


def test_systemd_source_symlink_is_rejected(
    atlas_paths,
    release_factory,
    tmp_path: Path,
) -> None:
    source, service = _service(atlas_paths, release_factory)
    unit = source / "init/systemd/refresh.service"
    unit.unlink()
    target = tmp_path / "unit"
    target.write_text(
        "[Unit]\n[Service]\n"
        "ExecStart=/opt/atlas/bin/atlas job instance run sample-instance\n",
        encoding="utf-8",
    )
    unit.symlink_to(target)
    with pytest.raises(ValueError, match="source artifact not found"):
        SystemdAdapter(
            atlas_paths.var / "systemd",
            jobs_dir=atlas_paths.jobs_dir,
        ).validate(service)


def test_systemd_job_instance_must_match_service_and_user(
    atlas_paths,
    release_factory,
) -> None:
    _, service = _service(atlas_paths, release_factory)
    adapter = SystemdAdapter(
        atlas_paths.var / "systemd",
        jobs_dir=atlas_paths.jobs_dir,
    )
    instance = atlas_paths.jobs_dir / "sample-instance.yml"
    instance.write_text(
        "schema: atlas.job-instance/v1\n"
        "release: worker\n"
        "job: refresh\n"
        "user: root\n"
        "working_directory: /tmp\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="User must match"):
        adapter.validate(service)

    instance.write_text(
        "schema: atlas.job-instance/v1\n"
        "release: other\n"
        "job: refresh\n"
        "user: ops\n"
        "working_directory: /tmp\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="service release and job"):
        adapter.validate(service)


def test_systemd_service_without_timer(
    atlas_paths,
    release_factory,
) -> None:
    source = release_factory(
        name="worker",
        commands=(),
        jobs=("refresh",),
        service="refresh",
    )
    manifest = yaml.safe_load((source / "release.yml").read_text(encoding="utf-8"))
    manifest["services"]["refresh"]["init"]["systemd"].pop("timer")
    (source / "release.yml").write_text(
        yaml.safe_dump(manifest, sort_keys=False),
        encoding="utf-8",
    )
    atlas_paths.jobs_dir.mkdir(parents=True)
    (atlas_paths.jobs_dir / "sample-instance.yml").write_text(
        "schema: atlas.job-instance/v1\n"
        "release: worker\n"
        "job: refresh\n"
        "user: ops\n"
        "working_directory: /tmp\n",
        encoding="utf-8",
    )
    install_release(
        source,
        atlas_paths.scripts_releases_root,
        atlas_paths.scripts_current_root,
    )
    service = resolve_service(
        atlas_paths.scripts_current_root,
        "worker",
        "refresh",
    )
    diff = SystemdAdapter(
        atlas_paths.var / "systemd",
        jobs_dir=atlas_paths.jobs_dir,
    ).diff(service)
    assert "atlas-worker-refresh.service" in diff
    assert "atlas-worker-refresh.timer" not in diff


def test_systemd_accepts_declared_command(
    atlas_paths,
    release_factory,
) -> None:
    adapter = SystemdAdapter(
        atlas_paths.var / "systemd",
        jobs_dir=atlas_paths.jobs_dir,
    )
    command_source = release_factory(
        name="reader",
        commands=("status-show",),
    )
    unit_root = command_source / "init/systemd"
    unit_root.mkdir(parents=True)
    (unit_root / "status.service").write_text(
        "[Unit]\nDescription=Status reader\n"
        "[Service]\n"
        "ExecStart=/opt/atlas/bin/atlas run status-show --verbose\n",
        encoding="utf-8",
    )
    manifest = yaml.safe_load(
        (command_source / "release.yml").read_text(encoding="utf-8")
    )
    manifest["services"]["status"] = {
        "command": "status-show",
        "init": {"systemd": {"service": "init/systemd/status.service"}},
    }
    (command_source / "release.yml").write_text(
        yaml.safe_dump(manifest, sort_keys=False),
        encoding="utf-8",
    )
    install_release(
        command_source,
        atlas_paths.scripts_releases_root,
        atlas_paths.scripts_current_root,
    )
    command_service = resolve_service(
        atlas_paths.scripts_current_root,
        "reader",
        "status",
    )
    adapter.validate(command_service)

    installed_unit = command_service.service.systemd.service
    installed_unit.write_text(
        "[Unit]\nDescription=Wrong command\n"
        "[Service]\n"
        "ExecStart=/opt/atlas/bin/atlas run another-command\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="declared command status-show"):
        adapter.validate(command_service)


def test_systemd_reload_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = SystemdAdapter(tmp_path)

    def missing(*args, **kwargs):
        raise FileNotFoundError("missing")

    monkeypatch.setattr("atlas.init.systemd.subprocess.run", missing)
    with pytest.raises(ValueError, match="systemctl command not found"):
        adapter.reload()

    def failed(command, check):
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr("atlas.init.systemd.subprocess.run", failed)
    with pytest.raises(ValueError, match="daemon-reload failed"):
        adapter.reload()


def test_atomic_install_cleans_temporary_file_on_replace_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.write_text("content", encoding="utf-8")
    destination = tmp_path / "destination"
    monkeypatch.setattr("atlas.init.systemd.os.fchown", lambda *args: None)

    def fail_replace(source_path, destination_path):
        raise OSError("replace failed")

    monkeypatch.setattr("atlas.init.systemd.os.replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        SystemdAdapter._atomic_install(source, destination)
    assert sorted(tmp_path.iterdir()) == [source]


def test_init_cli_uses_systemd_adapter(
    atlas_paths,
    release_factory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = release_factory(
        name="worker",
        commands=(),
        jobs=("refresh",),
        service="refresh",
    )
    other = release_factory(name="other", commands=("other-show",))
    install_release(
        source,
        atlas_paths.scripts_releases_root,
        atlas_paths.scripts_current_root,
    )
    install_release(
        other,
        atlas_paths.scripts_releases_root,
        atlas_paths.scripts_current_root,
    )
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
    assert capsys.readouterr().out == "worker\trefresh\tsystemd\n"
    assert cli.main(["init", "diff", "worker", "refresh"]) == 0
    assert capsys.readouterr().out == "unit diff\n"
    assert cli.main(["init", "install", "worker", "refresh"]) == 0
    assert "atlas-worker-refresh.service" in capsys.readouterr().out
    assert cli.main(["init", "remove", "worker", "refresh"]) == 0
    assert "atlas-worker-refresh.service" in capsys.readouterr().out
    assert calls == ["diff:refresh", "install:refresh", "remove:refresh"]
    with pytest.raises(ValueError, match="unknown release"):
        cli.main(["init", "list", "missing"])
