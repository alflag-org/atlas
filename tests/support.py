from __future__ import annotations

import sys
from pathlib import Path

import yaml

from atlas.launchers import publish_host_artifacts
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
    publish_host_artifacts(paths)
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
    modules = root / "modules"
    modules.mkdir(parents=True, exist_ok=True)
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
        module_name = command.replace("-", "_") + "_entry"
        module_file = modules / f"{module_name}.py"
        module_file.write_text(
            "from __future__ import annotations\n"
            "import os\n"
            "def main(argv: list[str] | None = None) -> None:\n"
            "    print(f\"{os.environ['ATLAS_ARTIFACT_NAME']}:"
            "{os.environ['ATLAS_RELEASE_NAME']}\")\n",
            encoding="utf-8",
        )
        command_entries[command] = {
            "target": f"{module_name}:main",
        }
    job_entries = manifest["jobs"]
    assert isinstance(job_entries, dict)
    for job in jobs:
        module_name = job.replace("-", "_") + "_entry"
        module_file = modules / f"{module_name}.py"
        module_file.write_text(
            "from __future__ import annotations\n"
            "import os\n"
            "import sys\n"
            "def main(argv: list[str] | None = None) -> None:\n"
            "    print(os.environ.get('TEST_JOB_VALUE', 'unset'))\n"
            "    print('|'.join(argv or []))\n",
            encoding="utf-8",
        )
        definition: dict[str, object] = {
            "target": f"{module_name}:main",
        }
        if timeout is not None:
            definition["default_timeout_seconds"] = timeout
        job_entries[job] = definition
    if service is not None:
        assert jobs
        unit_root = root / "init/systemd"
        unit_root.mkdir(parents=True)
        (unit_root / f"{service}.service").write_text(
            "[Unit]\nDescription=Sample\n"
            "[Service]\nUser=ops\n"
            "ExecStart=/opt/atlas/bin/atlas job instance run sample-instance\n",
            encoding="utf-8",
        )
        (unit_root / f"{service}.timer").write_text(
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
