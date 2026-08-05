"""Import and invoke one manifest-declared release callable."""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any

if __package__:
    from .target_contract import (
        TargetResolutionError,
        parse_target_spec,
        resolve_target_sources,
        validate_callable_runtime,
        validate_callable_source,
        validate_selected_module,
    )
else:
    _contract_spec = importlib.util.spec_from_file_location(
        "_atlas_target_contract",
        Path(__file__).with_name("target_contract.py"),
    )
    if _contract_spec is None or _contract_spec.loader is None:
        raise RuntimeError("Atlas target contract helper is unavailable")
    _contract_module = importlib.util.module_from_spec(_contract_spec)
    sys.modules["_atlas_target_contract"] = _contract_module
    _contract_spec.loader.exec_module(_contract_module)
    TargetResolutionError = _contract_module.TargetResolutionError
    parse_target_spec = _contract_module.parse_target_spec
    resolve_target_sources = _contract_module.resolve_target_sources
    validate_callable_source = _contract_module.validate_callable_source
    validate_callable_runtime = _contract_module.validate_callable_runtime
    validate_selected_module = _contract_module.validate_selected_module


def _target_parts(spec: str) -> tuple[str, str] | None:
    try:
        return parse_target_spec(spec)
    except ValueError:
        return None


def _module_is_selected(module: ModuleType) -> bool:
    release_root = os.environ.get("ATLAS_RELEASE_ROOT")
    module_name = getattr(module, "__name__", None)
    if not release_root or not isinstance(module_name, str):
        return False
    try:
        sources = resolve_target_sources(Path(release_root), module_name)
    except TargetResolutionError:
        return False
    return validate_selected_module(module, sources)


@contextmanager
def _selected_import_path(modules_root: Path) -> Iterator[None]:
    original = list(sys.path)
    incoming = {
        item
        for item in os.environ.get("PYTHONPATH", "").split(os.pathsep)
        if item
    }
    helper_root = str(Path(__file__).resolve().parent)
    selected_root = str(modules_root)
    sys.path[:] = [
        selected_root,
        helper_root,
        *(
            item
            for item in original
            if item not in incoming and item not in {selected_root, helper_root}
        ),
    ]
    try:
        yield
    finally:
        sys.path[:] = original


def _purge_target_modules(module_name: str, package_names: tuple[str, ...]) -> None:
    prefixes = (module_name, *package_names)
    for loaded_name in tuple(sys.modules):
        if any(
            loaded_name == prefix or loaded_name.startswith(f"{prefix}.")
            for prefix in prefixes
        ):
            sys.modules.pop(loaded_name, None)


def _load_module(source: Path, module_name: str, *, is_package: bool) -> ModuleType:
    search_locations = [str(source.parent)] if is_package else None
    spec = importlib.util.spec_from_file_location(
        module_name,
        source,
        submodule_search_locations=search_locations,
    )
    if spec is None or spec.loader is None:
        raise ValueError(f"target module could not be imported: {module_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException as exc:
        sys.modules.pop(module_name, None)
        raise ValueError(f"target module could not be imported: {module_name}") from exc
    return module


def _selected_target_sources(module_name: str):
    release_root = os.environ.get("ATLAS_RELEASE_ROOT")
    if not release_root:
        raise ValueError("selected release root is required")
    try:
        return resolve_target_sources(Path(release_root), module_name)
    except TargetResolutionError as exc:
        if exc.outside:
            raise ValueError(
                f"target module is outside the selected release: {module_name}"
            ) from exc
        if _external_target_source_exists(module_name, Path(release_root) / "modules"):
            raise ValueError(
                f"target module is outside the selected release: {module_name}"
            ) from exc
        raise ValueError(f"target module could not be imported: {module_name}") from exc


def _external_target_source_exists(module_name: str, selected_modules: Path) -> bool:
    parts = module_name.split(".")
    selected = selected_modules.resolve()
    for raw_root in sys.path:
        if not raw_root:
            continue
        root = Path(raw_root)
        try:
            if root.resolve() == selected:
                continue
        except OSError:
            continue
        module_path = root.joinpath(*parts)
        if (module_path.with_suffix(".py")).is_file() or (
            (module_path / "__init__.py").is_file()
        ):
            return True
    return False


def _load_callable(module_name: str, callable_name: str) -> Callable[[list[str]], Any]:
    sources = _selected_target_sources(module_name)
    validate_callable_source(sources.source, callable_name)
    package_names = tuple(name for name, _ in sources.package_sources)
    _purge_target_modules(module_name, package_names)
    with _selected_import_path(sources.modules_root):
        for package_name, package_source in sources.package_sources:
            _load_module(package_source, package_name, is_package=True)
        module = _load_module(
            sources.source,
            module_name,
            is_package=sources.is_package,
        )
        if not validate_selected_module(module, sources):
            raise ValueError(
                f"target module is outside the selected release: {module_name}"
            )
        target = getattr(module, callable_name, None)
        validate_callable_runtime(target, module_name, callable_name)
        return target


def run_target(spec: str, args: list[str]) -> int:
    """Import a selected release function and invoke it with ``args``."""
    parts = _target_parts(spec)
    if parts is None:
        raise ValueError(f"target must be package.module:callable: {spec}")
    module_name, callable_name = parts
    sources = _selected_target_sources(module_name)
    with _selected_import_path(sources.modules_root):
        target = _load_callable(module_name, callable_name)
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
