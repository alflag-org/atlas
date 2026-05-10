from __future__ import annotations

from pathlib import Path
import subprocess

from atlas.cli import main


def test_shims_symlink_to_script_runner(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True, exist_ok=True)
    (etc / "host.yml").write_text("name: t1\n", encoding="utf-8")

    monkeypatch.setenv("ATLAS_HOME", str(home))
    monkeypatch.setenv("ATLAS_ETC_DIR", str(etc))
    monkeypatch.setenv("ATLAS_VAR_DIR", str(var))
    monkeypatch.setenv("ATLAS_RUNTIME_DIR", str(home / "runtime"))
    monkeypatch.setenv("ATLAS_SCRIPTS_DIR", str(home / "scripts/current"))

    release_src = Path("examples/scripts-release").resolve()
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


def test_shim_executes_command(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True, exist_ok=True)
    (etc / "host.yml").write_text("name: t1\nsite: kng01\n", encoding="utf-8")

    monkeypatch.setenv("ATLAS_HOME", str(home))
    monkeypatch.setenv("ATLAS_ETC_DIR", str(etc))
    monkeypatch.setenv("ATLAS_VAR_DIR", str(var))
    monkeypatch.setenv("ATLAS_RUNTIME_DIR", str(home / "runtime"))
    monkeypatch.setenv("ATLAS_SCRIPTS_DIR", str(home / "scripts/current"))

    release_src = Path("examples/scripts-release").resolve()
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
