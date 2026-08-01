from __future__ import annotations

import hashlib
from pathlib import Path

from atlas_operations.operation.errors import InputError


def file_digest(path: str | Path) -> str:
    target = Path(path)
    if not target.is_file() or target.is_symlink():
        raise InputError(f"digest input is missing or unsafe: {target}")
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()
