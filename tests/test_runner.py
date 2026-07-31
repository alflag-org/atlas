from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from atlas.cli import main


def _set_env(monkeypatch, home: Path, etc: Path, var: Path) -> None:
    monkeypatch.setenv("ATLAS_HOME", str(home))
    monkeypatch.setenv("ATLAS_ETC_DIR", str(etc))
    monkeypatch.setenv("ATLAS_VAR_DIR", str(var))
    monkeypatch.setenv("ATLAS_RUNTIME_DIR", str(home / "runtime"))
    monkeypatch.setenv("ATLAS_SCRIPTS_CURRENT_DIR", str(home / "scripts/current"))
    monkeypatch.setenv("ATLAS_SCRIPTS_DIR", str(home / "scripts/current"))


def _write_release(path: Path, release_version: str, command_name: str, marker: str) -> Path:
    (path / "commands").mkdir(parents=True)
    (path / "modules").mkdir(parents=True)
    (path / "VERSION").write_text(f"{release_version}\n", encoding="utf-8")
    (path / "modules/sharedmod.py").write_text(f"IDENT = {marker!r}\n", encoding="utf-8")
    (path / "commands" / f"{command_name}.py").write_text(
        """
from __future__ import annotations

import json
import os
from pathlib import Path

from sharedmod import IDENT


def main() -> None:
    payload = {
        "release": os.environ["ATLAS_SCRIPT_RELEASE_NAME"],
        "scripts_dir": os.environ["ATLAS_SCRIPTS_DIR"],
        "current_dir": os.environ["ATLAS_SCRIPTS_CURRENT_DIR"],
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

    (etc / "host.yml").write_text("name: t1\nsite: kng01\n", encoding="utf-8")
    (etc / "config.yml").write_text(
        "runtime:\n  python:\n    version: '3.12'\nscripts:\n  source: ''\n", encoding="utf-8"
    )

    _set_env(monkeypatch, home, etc, var)
    scripts_python = home / "runtime/python/envs/scripts/bin/python"
    scripts_python.parent.mkdir(parents=True, exist_ok=True)
    python3 = shutil.which("python3")
    assert python3 is not None
    scripts_python.symlink_to(Path(python3))

    release_src = Path("examples/basic-scripts-release").resolve()
    assert main(["scripts", "install", str(release_src)]) == 0

    rc1 = main(["run", "sample", "hello", "--name=test"])
    assert rc1 == 0
    rc2 = main(["run", "group-nested-sample", "show-context"])
    assert rc2 == 0

    line = (var / "logs/runs.jsonl").read_text(encoding="utf-8").strip().splitlines()[-1]
    record = json.loads(line)
    assert record["release"] == "default"
    assert record["script"] == "group-nested-sample"
    assert record["exit_code"] == 0


@pytest.mark.skipif(shutil.which("python3") is None, reason="python3 is required")
def test_run_sets_release_env_and_pythonpath_order(monkeypatch, tmp_path: Path, capsys) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True, exist_ok=True)
    var.mkdir(parents=True, exist_ok=True)
    (etc / "host.yml").write_text("name: t1\nsite: kng01\n", encoding="utf-8")
    _set_env(monkeypatch, home, etc, var)
    monkeypatch.setenv("PYTHONPATH", "/existing/pythonpath")
    scripts_python = home / "runtime/python/envs/scripts/bin/python"
    scripts_python.parent.mkdir(parents=True, exist_ok=True)
    python3 = shutil.which("python3")
    assert python3 is not None
    scripts_python.symlink_to(Path(python3))

    alpha = _write_release(tmp_path / "alpha", "0.1.0", "alpha", "alpha")
    beta = _write_release(tmp_path / "beta", "0.2.0", "beta", "beta")
    assert main(["scripts", "install", str(alpha), "--name", "alpha"]) == 0
    assert main(["scripts", "install", str(beta), "--name", "beta"]) == 0

    assert main(["run", "alpha"]) == 0
    payload = json.loads((var / "runner-env.json").read_text(encoding="utf-8"))
    alpha_root = home / "scripts/releases/alpha/0.1.0"
    beta_root = home / "scripts/releases/beta/0.2.0"
    assert payload["release"] == "alpha"
    assert payload["scripts_dir"] == str(alpha_root)
    assert payload["current_dir"] == str(home / "scripts/current")
    assert payload["ident"] == "alpha"
    assert payload["pythonpath"][0] == str(alpha_root / "modules")
    assert payload["pythonpath"][1] == str(beta_root / "modules")
    assert payload["pythonpath"][-2] == str(home / "lib/python")
    assert payload["pythonpath"][-1] == "/existing/pythonpath"


def test_run_fails_on_command_collision(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True, exist_ok=True)
    var.mkdir(parents=True, exist_ok=True)
    (etc / "host.yml").write_text("name: t1\nsite: kng01\n", encoding="utf-8")
    _set_env(monkeypatch, home, etc, var)

    release_one = tmp_path / "release-one"
    release_two = tmp_path / "release-two"
    for root in [release_one, release_two]:
        (root / "commands").mkdir(parents=True)
        (root / "VERSION").write_text("0.1.0\n", encoding="utf-8")
        (root / "commands/collision.py").write_text("print('x')\n", encoding="utf-8")

    assert main(["scripts", "install", str(release_one), "--name", "one"]) == 0
    with pytest.raises(ValueError, match="command name collision: collision found in releases: one, two"):
        main(["scripts", "install", str(release_two), "--name", "two"])
    assert (home / "scripts/current/one").is_symlink()
    assert not (home / "scripts/current/two").exists()
    assert main(["scripts", "list"]) == 0


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

    _set_env(monkeypatch, home, etc, var)

    release_src = Path("examples/basic-scripts-release").resolve()
    assert main(["scripts", "install", str(release_src)]) == 0

    with pytest.raises(ValueError, match="scripts python executable not found"):
        main(["run", "sample", "hello", "--name=test"])
