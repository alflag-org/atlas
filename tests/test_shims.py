from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from atlas.cli import main
from atlas.launchers import regenerate_shims


def _set_env(monkeypatch, home: Path, etc: Path, var: Path) -> None:
    monkeypatch.setenv("ATLAS_HOME", str(home))
    monkeypatch.setenv("ATLAS_ETC_DIR", str(etc))
    monkeypatch.setenv("ATLAS_VAR_DIR", str(var))
    monkeypatch.setenv("ATLAS_RUNTIME_DIR", str(home / "runtime"))


def test_shims_symlink_to_artifact_runner(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True, exist_ok=True)
    (etc / "host.yml").write_text("name: t1\n", encoding="utf-8")

    _set_env(monkeypatch, home, etc, var)

    release_src = Path("examples/basic-release").resolve()
    assert main(["release", "install", str(release_src)]) == 0

    sample = home / "shims/sample"
    nested = home / "shims/group-nested-sample"
    runner = home / "bin/artifact-runner"
    assert sample.is_symlink()
    assert nested.is_symlink()
    assert sample.resolve() == runner
    assert nested.resolve() == runner

    content = runner.read_text(encoding="utf-8")
    assert f'exec "{home / "bin/atlas"}" run' in content


def test_regenerate_shims_removes_stale_files_and_preserves_directories(tmp_path: Path) -> None:
    current = tmp_path / "current"
    release = tmp_path / "releases/sample/0.1.0-sample-digest"
    (release / "modules").mkdir(parents=True)
    (release / "VERSION").write_text("0.1.0\n", encoding="utf-8")
    (release / "modules/sample.py").write_text(
        "def main(argv: list[str] | None = None) -> int:\n    return 0\n",
        encoding="utf-8",
    )
    (release / "release.yml").write_text(
        "schema: atlas.release/v1\n"
        "name: sample\n"
        "commands:\n"
        "  sample:\n"
        "    target: sample:main\n",
        encoding="utf-8",
    )
    current.mkdir(parents=True)
    (current / "sample").symlink_to(release, target_is_directory=True)
    shims = tmp_path / "shims"
    shims.mkdir()
    stale = shims / "old-command"
    stale.write_text("stale", encoding="utf-8")
    preserved = shims / "manual-dir"
    preserved.mkdir()
    runner = tmp_path / "artifact-runner"
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
    (etc / "host.yml").write_text("name: t1\nsite: site-a\n", encoding="utf-8")

    _set_env(monkeypatch, home, etc, var)
    runtime_python = home / "runtime/python/envs/scripts/bin/python"
    runtime_python.parent.mkdir(parents=True, exist_ok=True)
    python3 = shutil.which("python3")
    assert python3 is not None
    runtime_python.symlink_to(Path(python3))

    release_src = Path("examples/basic-release").resolve()
    assert main(["release", "install", str(release_src)]) == 0

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


def test_release_shims_fails_on_collision(monkeypatch, tmp_path: Path, capsys) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True, exist_ok=True)
    (etc / "host.yml").write_text("name: t1\n", encoding="utf-8")
    _set_env(monkeypatch, home, etc, var)

    for name in ["one", "two"]:
        release = tmp_path / name
        (release / "modules").mkdir(parents=True)
        (release / "VERSION").write_text("0.1.0\n", encoding="utf-8")
        (release / "modules/dup.py").write_text(
            "def main(argv: list[str] | None = None) -> int:\n    return 0\n",
            encoding="utf-8",
        )
        (release / "release.yml").write_text(
            "schema: atlas.release/v1\n"
            f"name: {name}\n"
            "commands:\n"
            "  dup:\n"
            "    target: dup:main\n",
            encoding="utf-8",
        )
        if name == "one":
            assert main(["release", "install", str(release)]) == 0
            continue
        assert main(["release", "install", str(release)]) == 2
        assert "command name collision: dup found in releases: one, two" in capsys.readouterr().err
