from __future__ import annotations

from pathlib import Path
import sys

import yaml

from atlas.paths import AtlasPaths, get_paths


def configure_paths(monkeypatch, tmp_path: Path) -> AtlasPaths:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    monkeypatch.setenv("ATLAS_HOME", str(home))
    monkeypatch.setenv("ATLAS_ETC_DIR", str(etc))
    monkeypatch.setenv("ATLAS_VAR_DIR", str(var))
    monkeypatch.setenv("ATLAS_RUNTIME_DIR", str(home / "runtime"))
    etc.mkdir(parents=True)
    (etc / "host.yml").write_text("name: test-host\nsite: test\n", encoding="utf-8")
    paths = get_paths()
    paths.runtime_python.parent.mkdir(parents=True)
    paths.runtime_python.symlink_to(sys.executable)
    return paths


def make_release(
    root: Path,
    *,
    name: str = "sample",
    version: str = "1.0.0",
    commands: tuple[str, ...] = ("sample-show",),
    jobs: tuple[str, ...] = (),
    timeout: int | None = None,
    service: str | None = None,
) -> Path:
    root.mkdir(parents=True)
    (root / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    manifest: dict[str, object] = {
        "schema": "atlas.release/v1",
        "name": name,
        "commands": {},
        "jobs": {},
        "services": {},
    }
    command_entries = manifest["commands"]
    assert isinstance(command_entries, dict)
    for command in commands:
        entrypoint = root / "commands" / f"{command}.py"
        entrypoint.parent.mkdir(parents=True, exist_ok=True)
        entrypoint.write_text(
            "from atlas_core import get_context\n"
            "ctx = get_context()\n"
            "print(f'{ctx.artifact.name}:{ctx.host.name}')\n",
            encoding="utf-8",
        )
        command_entries[command] = {
            "runtime": "python",
            "entrypoint": f"commands/{command}.py",
        }
    job_entries = manifest["jobs"]
    assert isinstance(job_entries, dict)
    for job in jobs:
        entrypoint = root / "jobs" / f"{job}.py"
        entrypoint.parent.mkdir(parents=True, exist_ok=True)
        entrypoint.write_text(
            "from __future__ import annotations\n"
            "import os\n"
            "import sys\n"
            "print(os.environ.get('TEST_JOB_VALUE', 'unset'))\n"
            "print('|'.join(sys.argv[1:]))\n",
            encoding="utf-8",
        )
        definition: dict[str, object] = {
            "runtime": "python",
            "entrypoint": f"jobs/{job}.py",
        }
        if timeout is not None:
            definition["default_timeout_seconds"] = timeout
        job_entries[job] = definition
    if service is not None:
        assert jobs
        unit_root = root / "init/systemd"
        unit_root.mkdir(parents=True)
        service_source = unit_root / f"{service}.service"
        service_source.write_text(
            "[Unit]\nDescription=Sample\n"
            "[Service]\nUser=ops\n"
            "ExecStart=/opt/atlas/bin/atlas job instance run sample-instance\n",
            encoding="utf-8",
        )
        timer_source = unit_root / f"{service}.timer"
        timer_source.write_text(
            "[Unit]\nDescription=Sample timer\n"
            "[Timer]\nOnCalendar=hourly\n"
            f"Unit=atlas-{name}-{service}.service\n",
            encoding="utf-8",
        )
        service_entries = manifest["services"]
        assert isinstance(service_entries, dict)
        service_entries[service] = {
            "job": jobs[0],
            "init": {
                "systemd": {
                    "service": f"init/systemd/{service}.service",
                    "timer": f"init/systemd/{service}.timer",
                }
            },
        }
    (root / "release.yml").write_text(
        yaml.safe_dump(manifest, sort_keys=False),
        encoding="utf-8",
    )
    return root
