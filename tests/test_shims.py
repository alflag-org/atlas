from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from atlas.cli import main
from atlas.launchers import publish_host_artifacts
from atlas.paths import ensure_dirs, get_paths
from atlas.releases import install_release


def _set_env(monkeypatch, home: Path, etc: Path, var: Path) -> None:
    monkeypatch.setenv("ATLAS_HOME", str(home))
    monkeypatch.setenv("ATLAS_ETC_DIR", str(etc))
    monkeypatch.setenv("ATLAS_VAR_DIR", str(var))
    monkeypatch.setenv("ATLAS_RUNTIME_DIR", str(home / "runtime"))
    runtime_python = home / "runtime/python/envs/scripts/bin/python"
    runtime_python.parent.mkdir(parents=True)
    runtime_python.symlink_to(Path(sys.executable))


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


def test_publish_host_artifacts_removes_stale_files_and_preserves_directories(
    monkeypatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    monkeypatch.setenv("ATLAS_HOME", str(home))
    monkeypatch.setenv("ATLAS_ETC_DIR", str(etc))
    monkeypatch.setenv("ATLAS_VAR_DIR", str(var))
    monkeypatch.setenv("ATLAS_RUNTIME_DIR", str(home / "runtime"))
    paths = get_paths()
    ensure_dirs(paths)
    current = paths.current_root
    release = tmp_path / "source"
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
    releases = paths.releases_root
    install_release(release, releases, current)
    shims = paths.shims
    shims.mkdir()
    stale = shims / "old-command"
    stale.write_text("stale", encoding="utf-8")
    preserved = shims / "manual-dir"
    preserved.mkdir()
    names = publish_host_artifacts(paths)

    assert names == ["sample"]
    assert not stale.exists()
    assert preserved.is_dir()
    assert (shims / "sample").resolve() == paths.artifact_runner


def test_shim_executes_command(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True, exist_ok=True)
    (etc / "host.yml").write_text("name: t1\nsite: site-a\n", encoding="utf-8")

    _set_env(monkeypatch, home, etc, var)
    runtime_python = home / "runtime/python/envs/scripts/bin/python"
    runtime_python.unlink()
    runtime_python.symlink_to(Path(sys.executable))

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


def test_concurrent_release_refreshes_publish_complete_generations(
    monkeypatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True, exist_ok=True)
    (etc / "host.yml").write_text("name: t1\n", encoding="utf-8")
    _set_env(monkeypatch, home, etc, var)

    sources: list[Path] = []
    for name in ("alpha", "bravo"):
        source = tmp_path / f"source-{name}"
        (source / "modules").mkdir(parents=True)
        (source / "VERSION").write_text("1.0.0\n", encoding="utf-8")
        (source / f"modules/{name}.py").write_text(
            "def main(argv: list[str] | None = None) -> int:\n"
            "    return 0\n",
            encoding="utf-8",
        )
        (source / "release.yml").write_text(
            "schema: atlas.release/v1\n"
            f"name: {name}\n"
            "commands:\n"
            f"  {name}:\n"
            f"    target: {name}:main\n",
            encoding="utf-8",
        )
        sources.append(source)
    sources.append(sources[0])

    repository = Path(__file__).resolve().parents[1]
    child_env = os.environ.copy()
    child_env["PYTHONPATH"] = os.pathsep.join(
        [str(repository / "src"), str(repository / "operations/modules")]
    )
    command = (
        "import sys\n"
        "from atlas.cli import main\n"
        "raise SystemExit(main(['release', 'install', sys.argv[1]]))\n"
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", command, str(source)],
            cwd=repository,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for source in sources
    ]
    deadline = time.monotonic() + 10
    while any(process.poll() is None for process in processes):
        current = home / "artifacts/current"
        if current.is_symlink():
            generation = current.resolve()
            assert (generation / "python/atlas_core").is_dir()
            assert (generation / "python/atlas_release_runner.py").is_file()
            assert (generation / "python/target_contract.py").is_file()
            assert (generation / "shims").is_dir()
            for shim in (generation / "shims").iterdir():
                if shim.is_symlink():
                    assert shim.resolve() == home / "bin/artifact-runner"
        assert time.monotonic() < deadline
        time.sleep(0.005)

    results = [process.communicate(timeout=5) for process in processes]
    assert [process.returncode for process in processes] == [0, 0, 0], results
    generation = (home / "artifacts/current").resolve()
    assert (generation / "shims/alpha").is_symlink()
    assert (generation / "shims/bravo").is_symlink()
    assert (home / "shims/alpha").resolve() == home / "bin/artifact-runner"
    assert (home / "shims/bravo").resolve() == home / "bin/artifact-runner"
