from __future__ import annotations

import sys
from pathlib import Path

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
    monkeypatch.setenv("ATLAS_SCRIPTS_CURRENT_DIR", str(home / "scripts/current"))
    monkeypatch.setenv("ATLAS_SCRIPTS_DIR", str(home / "scripts/current"))
    etc.mkdir(parents=True)
    (etc / "host.yml").write_text("name: test-host\nsite: test\n", encoding="utf-8")
    paths = get_paths()
    paths.scripts_python.parent.mkdir(parents=True)
    paths.scripts_python.symlink_to(sys.executable)
    return paths


def make_release(
    root: Path,
    *,
    name: str = "sample",
    version: str = "1.0.0",
    commands: tuple[str, ...] = ("sample-show",),
    jobs: tuple[str, ...] = (),
    timeout: int | None = None,
) -> Path:
    root.mkdir(parents=True)
    (root / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    manifest: dict[str, object] = {
        "schema": "atlas.release/v1",
        "name": name,
        "commands": {},
        "jobs": {},
    }
    command_entries = manifest["commands"]
    assert isinstance(command_entries, dict)
    for command in commands:
        entrypoint = root / "commands" / f"{command}.py"
        entrypoint.parent.mkdir(parents=True, exist_ok=True)
        entrypoint.write_text(
            "from __future__ import annotations\n"
            "import os\n"
            "print(f\"{os.environ['ATLAS_SCRIPT_NAME']}:"
            "{os.environ['ATLAS_SCRIPT_RELEASE_NAME']}\")\n",
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
    (root / "release.yml").write_text(
        yaml.safe_dump(manifest, sort_keys=False),
        encoding="utf-8",
    )
    return root
