from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

from atlas.cli import main
from atlas.launchers import regenerate_shims


def _set_env(monkeypatch, home: Path, etc: Path, var: Path) -> None:
    monkeypatch.setenv("ATLAS_HOME", str(home))
    monkeypatch.setenv("ATLAS_ETC_DIR", str(etc))
    monkeypatch.setenv("ATLAS_VAR_DIR", str(var))
    monkeypatch.setenv("ATLAS_RUNTIME_DIR", str(home / "runtime"))
    monkeypatch.setenv("ATLAS_SCRIPTS_CURRENT_DIR", str(home / "scripts/current"))
    monkeypatch.setenv("ATLAS_SCRIPTS_DIR", str(home / "scripts/current"))


def test_shims_symlink_to_script_runner(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True, exist_ok=True)
    (etc / "host.yml").write_text("name: t1\n", encoding="utf-8")

    _set_env(monkeypatch, home, etc, var)

    release_src = Path("examples/basic-scripts-release").resolve()
    assert main(["scripts", "install", str(release_src)]) == 0

    sample = home / "shims/sample"
    nested = home / "shims/group-nested-sample"
    runner = home / "bin/script-runner"
    assert sample.is_symlink()
    assert nested.is_symlink()
    assert sample.resolve() == runner
    assert nested.resolve() == runner

    content = runner.read_text(encoding="utf-8")
    assert f'exec "{home / "bin/atlas"}" run' in content


def test_regenerate_shims_removes_stale_files_and_preserves_directories(tmp_path: Path) -> None:
    current = tmp_path / "scripts/current"
    release = tmp_path / "scripts/releases/sample/0.1.0"
    (release / "commands").mkdir(parents=True)
    (release / "VERSION").write_text("0.1.0\n", encoding="utf-8")
    (release / "commands/sample.py").write_text("print('sample')\n", encoding="utf-8")
    current.mkdir(parents=True)
    (current / "sample").symlink_to(release, target_is_directory=True)
    shims = tmp_path / "shims"
    shims.mkdir()
    stale = shims / "old-command"
    stale.write_text("stale", encoding="utf-8")
    preserved = shims / "manual-dir"
    preserved.mkdir()
    runner = tmp_path / "script-runner"
    runner.write_text("#!/usr/bin/env bash\n", encoding="utf-8")

    names = regenerate_shims(current, shims, runner)

    assert names == ["sample"]
    assert not stale.exists()
    assert preserved.is_dir()
    assert (shims / "sample").resolve() == runner


def test_shim_executes_command(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True, exist_ok=True)
    (etc / "host.yml").write_text("name: t1\nsite: kng01\n", encoding="utf-8")

    _set_env(monkeypatch, home, etc, var)
    scripts_python = home / "runtime/python/envs/scripts/bin/python"
    scripts_python.parent.mkdir(parents=True, exist_ok=True)
    python3 = shutil.which("python3")
    assert python3 is not None
    scripts_python.symlink_to(Path(python3))

    release_src = Path("examples/basic-scripts-release").resolve()
    assert main(["scripts", "install", str(release_src)]) == 0

    import os
    env = dict(os.environ)
    env["PATH"] = f"{home / 'shims'}:{env.get('PATH', '')}"
    proc = subprocess.run(
        ["sample", "hello", "--name=test"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0
    assert "[sample] hello test" in proc.stdout


def test_scripts_shims_fails_on_collision(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True, exist_ok=True)
    (etc / "host.yml").write_text("name: t1\n", encoding="utf-8")
    _set_env(monkeypatch, home, etc, var)

    for name in ["one", "two"]:
        release = tmp_path / name
        (release / "commands").mkdir(parents=True)
        (release / "VERSION").write_text("0.1.0\n", encoding="utf-8")
        (release / "commands/dup.py").write_text("print('dup')\n", encoding="utf-8")
        if name == "one":
            assert main(["scripts", "install", str(release), "--name", name]) == 0
            continue
        with pytest.raises(ValueError, match="command name collision: dup found in releases: one, two"):
            main(["scripts", "install", str(release), "--name", name])
