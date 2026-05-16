from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest

from atlas.cli import main


@pytest.mark.skipif(shutil.which("python3") is None, reason="python3 is required")
def test_run_and_logs(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True, exist_ok=True)
    var.mkdir(parents=True, exist_ok=True)

    (etc / "host.yml").write_text("name: t1\nsite: kng01\n", encoding="utf-8")
    (etc / "config.yml").write_text(
        "runtime:\n  python:\n    version: '3.12'\nscripts:\n  source: ''\n", encoding="utf-8"
    )

    monkeypatch.setenv("ATLAS_HOME", str(home))
    monkeypatch.setenv("ATLAS_ETC_DIR", str(etc))
    monkeypatch.setenv("ATLAS_VAR_DIR", str(var))
    monkeypatch.setenv("ATLAS_RUNTIME_DIR", str(home / "runtime"))
    monkeypatch.setenv("ATLAS_SCRIPTS_DIR", str(home / "scripts/current"))
    scripts_python = home / "runtime/python/envs/scripts/bin/python"
    scripts_python.parent.mkdir(parents=True, exist_ok=True)
    python3 = shutil.which("python3")
    assert python3 is not None
    scripts_python.symlink_to(Path(python3))

    release_src = Path("examples/scripts-release").resolve()
    assert main(["scripts", "install", str(release_src)]) == 0

    rc1 = main(["run", "sample", "hello", "--name=test"])
    assert rc1 == 0
    rc2 = main(["run", "group-nested-sample", "show-context"])
    assert rc2 == 0

    line = (var / "logs/runs.jsonl").read_text(encoding="utf-8").strip().splitlines()[-1]
    record = json.loads(line)
    assert record["script"] == "group-nested-sample"
    assert record["exit_code"] == 0


def test_run_fails_when_scripts_python_is_missing(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True, exist_ok=True)
    var.mkdir(parents=True, exist_ok=True)

    (etc / "host.yml").write_text("name: t1\nsite: kng01\n", encoding="utf-8")
    (etc / "config.yml").write_text(
        "runtime:\n  python:\n    version: '3.12'\nscripts:\n  source: ''\n", encoding="utf-8"
    )

    monkeypatch.setenv("ATLAS_HOME", str(home))
    monkeypatch.setenv("ATLAS_ETC_DIR", str(etc))
    monkeypatch.setenv("ATLAS_VAR_DIR", str(var))
    monkeypatch.setenv("ATLAS_RUNTIME_DIR", str(home / "runtime"))
    monkeypatch.setenv("ATLAS_SCRIPTS_DIR", str(home / "scripts/current"))

    release_src = Path("examples/scripts-release").resolve()
    assert main(["scripts", "install", str(release_src)]) == 0

    with pytest.raises(ValueError, match="scripts python executable not found"):
        main(["run", "sample", "hello", "--name=test"])
