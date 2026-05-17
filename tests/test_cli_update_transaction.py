from __future__ import annotations

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


def _write_release(path: Path, release_version: str, command_name: str) -> Path:
    (path / "commands").mkdir(parents=True, exist_ok=True)
    (path / "modules").mkdir(parents=True, exist_ok=True)
    (path / "VERSION").write_text(f"{release_version}\n", encoding="utf-8")
    (path / "commands" / f"{command_name}.py").write_text("print('ok')\n", encoding="utf-8")
    return path


def test_scripts_update_rolls_back_all_target_releases_on_collision(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True, exist_ok=True)
    var.mkdir(parents=True, exist_ok=True)
    _set_env(monkeypatch, home, etc, var)

    old_one = _write_release(tmp_path / "old-one", "0.1.0", "one-cmd")
    old_two = _write_release(tmp_path / "old-two", "0.2.0", "two-cmd")
    assert main(["scripts", "install", str(old_one), "--name", "one"]) == 0
    assert main(["scripts", "install", str(old_two), "--name", "two"]) == 0

    old_one_target = home / "scripts/releases/one/0.1.0"
    old_two_target = home / "scripts/releases/two/0.2.0"
    assert (home / "scripts/current/one").resolve() == old_one_target
    assert (home / "scripts/current/two").resolve() == old_two_target

    new_one = _write_release(tmp_path / "new-one", "0.3.0", "dup")
    new_two = _write_release(tmp_path / "new-two", "0.4.0", "dup")
    (etc / "config.yml").write_text(
        f"""
runtime:
  python:
    version: "3.12.8"
scripts:
  releases:
    one:
      source: "file://{new_one}"
    two:
      source: "file://{new_two}"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="command name collision: dup found in releases: one, two"):
        main(["scripts", "update"])

    assert (home / "scripts/current/one").resolve() == old_one_target
    assert (home / "scripts/current/two").resolve() == old_two_target
    assert main(["scripts", "list"]) == 0


def test_scripts_update_single_release_rolls_back_on_collision(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True, exist_ok=True)
    var.mkdir(parents=True, exist_ok=True)
    _set_env(monkeypatch, home, etc, var)

    old_one = _write_release(tmp_path / "old-one", "0.1.0", "one-cmd")
    old_two = _write_release(tmp_path / "old-two", "0.2.0", "two-cmd")
    assert main(["scripts", "install", str(old_one), "--name", "one"]) == 0
    assert main(["scripts", "install", str(old_two), "--name", "two"]) == 0

    old_one_target = home / "scripts/releases/one/0.1.0"
    old_two_target = home / "scripts/releases/two/0.2.0"
    assert (home / "scripts/current/one").resolve() == old_one_target
    assert (home / "scripts/current/two").resolve() == old_two_target

    new_one = _write_release(tmp_path / "new-one", "0.3.0", "two-cmd")
    (etc / "config.yml").write_text(
        f"""
runtime:
  python:
    version: "3.12.8"
scripts:
  releases:
    one:
      source: "file://{new_one}"
    two:
      source: "file://{old_two}"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="command name collision: two-cmd found in releases: one, two"):
        main(["scripts", "update", "one"])

    assert (home / "scripts/current/one").resolve() == old_one_target
    assert (home / "scripts/current/two").resolve() == old_two_target
    assert main(["scripts", "list"]) == 0
