"""Import and invoke one manifest-declared release callable."""

from __future__ import annotations

import importlib
import inspect
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

TARGET_RE = re.compile(
    r"^(?P<module>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*):"
    r"(?P<callable>[A-Za-z_][A-Za-z0-9_]*)$"
)


def _target_parts(spec: str) -> tuple[str, str] | None:
    match = TARGET_RE.fullmatch(spec)
    if match is None:
        return None
    return match.group("module"), match.group("callable")


def _module_is_selected(module: ModuleType) -> bool:
    origin = getattr(module, "__file__", None)
    release_root = os.environ.get("ATLAS_RELEASE_ROOT")
    if not isinstance(origin, str) or not release_root:
        return False
    source = Path(origin).resolve()
    modules_root = (Path(release_root) / "modules").resolve()
    return modules_root in source.parents and source.is_file()


def _load_callable(module_name: str, callable_name: str) -> Callable[[list[str]], Any]:
    try:
        module = importlib.import_module(module_name)
    except (ImportError, ModuleNotFoundError) as exc:
        raise ValueError(f"target module could not be imported: {module_name}") from exc
    if not _module_is_selected(module):
        raise ValueError(f"target module is outside the selected release: {module_name}")
    target = getattr(module, callable_name, None)
    if not inspect.isfunction(target):
        raise ValueError(f"target is not a function: {module_name}:{callable_name}")
    try:
        inspect.signature(target).bind([])
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"target callable must accept argv: {module_name}:{callable_name}"
        ) from exc
    return target


def run_target(spec: str, args: list[str]) -> int:
    """Import a selected release function and invoke it with ``args``."""
    parts = _target_parts(spec)
    if parts is None:
        raise ValueError(f"target must be package.module:callable: {spec}")
    target = _load_callable(*parts)
    result = target(args)
    if result is None:
        return 0
    if isinstance(result, bool) or not isinstance(result, int):
        raise TypeError(f"target must return int or None: {spec}")
    return result


def main(argv: list[str] | None = None) -> int:
    """Run the target named by the first argument."""
    supplied = sys.argv[1:] if argv is None else argv
    if not supplied:
        raise ValueError("target is required")
    return run_target(supplied[0], supplied[1:])


if __name__ == "__main__":
    raise SystemExit(main())
