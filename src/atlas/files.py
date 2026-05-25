"""Filesystem helpers used by Atlas operations."""

from __future__ import annotations

from pathlib import Path
import shutil


def remove_path(path: Path) -> None:
    """Remove a file, symlink, or directory tree if it exists."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)
