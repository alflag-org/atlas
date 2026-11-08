from __future__ import annotations

from pathlib import Path

import pytest

from atlas.config import load_config
from atlas.paths import ensure_dirs, get_paths
from atlas.shims import generate_shims

from .support import write_python_command


def test_generate_shims_replaces_stale_and_preserves_non_atlas_files(tmp_path: Path) -> None:
    root = tmp_path / "program"
    write_python_command(root)
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        f"programs:\n  tool:\n    root: {root}\n    runtime:\n      type: python\n      venv: tool\n",
        encoding="utf-8",
    )
    config = load_config(config_path)
    paths = get_paths(
        {
            "ATLAS_HOME": str(tmp_path / "atlas"),
            "ATLAS_ETC_DIR": str(tmp_path / "etc"),
            "ATLAS_VAR_DIR": str(tmp_path / "var"),
        }
    )
    ensure_dirs(paths)
    (paths.shims / "manual").write_text("manual\n", encoding="utf-8")
    stale = paths.shims / "stale"
    stale.write_text("#!/bin/sh\n# atlas-shim: generated\n", encoding="utf-8")

    names = generate_shims(paths, config)

    assert names == ["sample"]
    assert (paths.shims / "sample").stat().st_mode & 0o111
    assert not stale.exists()
    assert (paths.shims / "manual").exists()


def test_shim_generation_rejects_manual_collision_and_command_collision(tmp_path: Path) -> None:
    root = tmp_path / "program"
    write_python_command(root)
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        f"programs:\n  tool:\n    root: {root}\n    runtime:\n      type: python\n      venv: tool\n",
        encoding="utf-8",
    )
    config = load_config(config_path)
    paths = get_paths(
        {
            "ATLAS_HOME": str(tmp_path / "atlas"),
            "ATLAS_ETC_DIR": str(tmp_path / "etc"),
            "ATLAS_VAR_DIR": str(tmp_path / "var"),
        }
    )
    ensure_dirs(paths)
    (paths.shims / "sample").write_text("manual\n", encoding="utf-8")
    with pytest.raises(ValueError, match="non-Atlas"):
        generate_shims(paths, config)

    for path in paths.shims.iterdir():
        path.unlink()
    paths.shims.rmdir()
    paths.shims.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ValueError, match="shims path"):
        generate_shims(paths, config)


def test_shim_generation_rejects_directory_at_command_path(tmp_path: Path) -> None:
    root = tmp_path / "program"
    write_python_command(root)
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        f"programs:\n  tool:\n    root: {root}\n    runtime:\n      type: python\n      venv: tool\n",
        encoding="utf-8",
    )
    paths = get_paths(
        {
            "ATLAS_HOME": str(tmp_path / "atlas"),
            "ATLAS_ETC_DIR": str(tmp_path / "etc"),
            "ATLAS_VAR_DIR": str(tmp_path / "var"),
        }
    )
    ensure_dirs(paths)
    (paths.shims / "sample").mkdir()
    with pytest.raises(ValueError, match="regular file"):
        generate_shims(paths, load_config(config_path))
