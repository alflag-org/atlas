from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from atlas.cli import main


def _set_env(monkeypatch, home: Path, etc: Path, var: Path) -> None:
    monkeypatch.setenv("ATLAS_HOME", str(home))
    monkeypatch.setenv("ATLAS_ETC_DIR", str(etc))
    monkeypatch.setenv("ATLAS_VAR_DIR", str(var))
    monkeypatch.setenv("ATLAS_RUNTIME_DIR", str(home / "runtime"))
    if not (etc / "config.yml").exists():
        (etc / "config.yml").write_text(
            f"runtime:\n  python:\n    version: '{sys.version_info.major}.{sys.version_info.minor}'\n"
            "releases: {}\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(
        "atlas.runtime._ensure_pyenv_runtime",
        lambda version, env=None: Path(sys.executable),
    )


def _write_release(path: Path, release_version: str, command_name: str, marker: str) -> Path:
    (path / "modules").mkdir(parents=True)
    (path / "VERSION").write_text(f"{release_version}\n", encoding="utf-8")
    (path / "release.yml").write_text(
        "schema: atlas.release/v1\n"
        f"name: {path.name}\n"
        "commands:\n"
        f"  {command_name}:\n"
        f"    target: {command_name}_entry:main\n",
        encoding="utf-8",
    )
    (path / "modules/sharedmod.py").write_text(f"IDENT = {marker!r}\n", encoding="utf-8")
    (path / "modules" / f"{command_name}_entry.py").write_text(
        """
from __future__ import annotations

import json
import os
from pathlib import Path

from sharedmod import IDENT


def main(argv: list[str] | None = None) -> None:
    payload = {
        "release": os.environ["ATLAS_RELEASE_NAME"],
        "release_root": os.environ["ATLAS_RELEASE_ROOT"],
        "artifact": os.environ["ATLAS_ARTIFACT_NAME"],
        "artifact_type": os.environ["ATLAS_ARTIFACT_TYPE"],
        "legacy_present": any(name.startswith("ATLAS_SCRIPT") or name.startswith("ATLAS_SCRIPTS") for name in os.environ),
        "ident": IDENT,
        "pythonpath": os.environ["PYTHONPATH"].split(":"),
    }
    out_path = Path(os.environ["ATLAS_VAR_DIR"]) / "runner-env.json"
    out_path.write_text(json.dumps(payload), encoding="utf-8")
    print(json.dumps(payload))


if __name__ == "__main__":
    main()
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.skipif(shutil.which("python3") is None, reason="python3 is required")
def test_run_and_logs(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True, exist_ok=True)
    var.mkdir(parents=True, exist_ok=True)

    (etc / "host.yml").write_text("name: t1\nsite: site-a\n", encoding="utf-8")
    (etc / "config.yml").write_text(
        "runtime:\n  python:\n    version: '3.12'\nreleases: {}\n",
        encoding="utf-8",
    )

    _set_env(monkeypatch, home, etc, var)
    runtime_python = home / "runtime/python/envs/scripts/bin/python"
    runtime_python.parent.mkdir(parents=True, exist_ok=True)
    runtime_python.symlink_to(Path(sys.executable))

    release_src = Path("examples/basic-release").resolve()
    assert main(["release", "install", str(release_src)]) == 0

    rc1 = main(["run", "sample", "hello", "--name=test"])
    assert rc1 == 0
    rc2 = main(["run", "group-nested-sample", "show-context"])
    assert rc2 == 0

    line = (var / "logs/runs.jsonl").read_text(encoding="utf-8").strip().splitlines()[-1]
    record = json.loads(line)
    assert record["release"] == "sample"
    assert record["artifact_type"] == "command"
    assert record["artifact"] == "group-nested-sample"
    assert record["exit_code"] == 0


@pytest.mark.skipif(shutil.which("python3") is None, reason="python3 is required")
def test_run_sets_release_env_and_pythonpath_order(monkeypatch, tmp_path: Path, capsys) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True, exist_ok=True)
    var.mkdir(parents=True, exist_ok=True)
    (etc / "host.yml").write_text("name: t1\nsite: site-a\n", encoding="utf-8")
    _set_env(monkeypatch, home, etc, var)
    monkeypatch.setenv("PYTHONPATH", "/existing/pythonpath")
    runtime_python = home / "runtime/python/envs/scripts/bin/python"
    runtime_python.parent.mkdir(parents=True, exist_ok=True)
    runtime_python.symlink_to(Path(sys.executable))

    alpha = _write_release(tmp_path / "alpha", "0.1.0", "alpha", "alpha")
    beta = _write_release(tmp_path / "beta", "0.2.0", "beta", "beta")
    assert main(["release", "install", str(alpha)]) == 0
    assert main(["release", "install", str(beta)]) == 0

    assert main(["run", "alpha"]) == 0
    payload = json.loads((var / "runner-env.json").read_text(encoding="utf-8"))
    alpha_root = (home / "current/alpha").resolve()
    assert payload["release"] == "alpha"
    assert payload["release_root"] == str(alpha_root)
    assert payload["artifact"] == "alpha"
    assert payload["artifact_type"] == "command"
    assert payload["legacy_present"] is False
    assert payload["ident"] == "alpha"
    assert payload["pythonpath"][0] == str(alpha_root / "modules")
    assert payload["pythonpath"][1] == str((home / "artifacts/current").resolve() / "python")
    assert payload["pythonpath"] == [
        str(alpha_root / "modules"),
        str((home / "artifacts/current").resolve() / "python"),
    ]


def test_run_fails_on_command_collision(monkeypatch, tmp_path: Path, capsys) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True, exist_ok=True)
    var.mkdir(parents=True, exist_ok=True)
    (etc / "host.yml").write_text("name: t1\nsite: site-a\n", encoding="utf-8")
    _set_env(monkeypatch, home, etc, var)

    release_one = tmp_path / "release-one"
    release_two = tmp_path / "release-two"
    for root, release_name in [(release_one, "one"), (release_two, "two")]:
        (root / "modules").mkdir(parents=True)
        (root / "VERSION").write_text("0.1.0\n", encoding="utf-8")
        (root / "modules/collision.py").write_text(
            "def main(argv: list[str] | None = None) -> int:\n    return 0\n",
            encoding="utf-8",
        )
        (root / "release.yml").write_text(
            "schema: atlas.release/v1\n"
            f"name: {release_name}\n"
            "commands:\n"
            "  collision:\n"
            "    target: collision:main\n",
            encoding="utf-8",
        )

    assert main(["release", "install", str(release_one)]) == 0
    assert main(["release", "install", str(release_two)]) == 2
    assert "command name collision: collision found in releases: one, two" in capsys.readouterr().err
    assert (home / "current/one").is_symlink()
    assert not (home / "current/two").exists()
    assert main(["release", "list"]) == 0


def test_run_fails_when_runtime_python_is_missing(monkeypatch, tmp_path: Path, capsys) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True, exist_ok=True)
    var.mkdir(parents=True, exist_ok=True)

    (etc / "host.yml").write_text("name: t1\nsite: site-a\n", encoding="utf-8")
    (etc / "config.yml").write_text(
        "runtime:\n  python:\n    version: '3.12'\nreleases: {}\n",
        encoding="utf-8",
    )

    _set_env(monkeypatch, home, etc, var)

    release_src = Path("examples/basic-release").resolve()
    runtime_python = home / "runtime/python/envs/scripts/bin/python"
    runtime_python.parent.mkdir(parents=True, exist_ok=True)
    runtime_python.symlink_to(Path(sys.executable))
    assert main(["release", "install", str(release_src)]) == 0
    runtime_python.unlink()

    assert main(["run", "sample", "hello", "--name=test"]) == 2
    assert "runtime python executable not found" in capsys.readouterr().err
