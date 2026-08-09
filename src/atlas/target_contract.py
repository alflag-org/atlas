"""Pure target resolution and callable-contract checks for release code."""

from __future__ import annotations

import ast
import inspect
import re
import types
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Union, get_args, get_origin

TARGET_RE = re.compile(
    r"^(?P<module>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*):"
    r"(?P<callable>[A-Za-z_][A-Za-z0-9_]*)$"
)


class TargetResolutionError(ValueError):
    """A target cannot be proven to belong to the selected release."""

    def __init__(self, message: str, *, outside: bool) -> None:
        super().__init__(message)
        self.outside = outside


@dataclass(frozen=True)
class TargetSources:
    """Selected source files for a dotted target and all of its parents."""

    modules_root: Path
    module_name: str
    package_sources: tuple[tuple[str, Path], ...]
    source: Path
    is_package: bool


def parse_target_spec(spec: str) -> tuple[str, str]:
    """Parse a manifest target without importing any release code."""
    if not isinstance(spec, str) or spec != spec.strip():
        raise ValueError("target must be package.module:callable")
    match = TARGET_RE.fullmatch(spec)
    if match is None:
        raise ValueError("target must be package.module:callable")
    return match.group("module"), match.group("callable")


def _selected_path(path: Path, modules_root: Path, label: str) -> Path:
    current = path
    while current != modules_root:
        if current.is_symlink():
            raise TargetResolutionError(
                f"target {label} must not contain a symlink",
                outside=True,
            )
        if modules_root not in current.parents:
            raise TargetResolutionError(
                f"target {label} escapes the release root",
                outside=True,
            )
        current = current.parent
    resolved_root = modules_root.resolve()
    resolved = path.resolve()
    if resolved_root not in resolved.parents:
        raise TargetResolutionError(
            f"target {label} escapes the release root",
            outside=True,
        )
    return resolved


def _regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def resolve_target_sources(release_root: Path, module_name: str) -> TargetSources:
    """Resolve a target module and every dotted parent below ``modules/``."""
    if release_root.is_symlink() or not release_root.is_dir():
        raise TargetResolutionError(
            "selected release root is not a directory",
            outside=True,
        )
    modules_root = release_root / "modules"
    if modules_root.is_symlink():
        raise TargetResolutionError(
            "target module root must not contain a symlink",
            outside=True,
        )
    if not modules_root.is_dir():
        raise TargetResolutionError(
            f"target module root not found: {modules_root}",
            outside=True,
        )
    modules_root = modules_root.resolve()
    parts = module_name.split(".")
    package_sources: list[tuple[str, Path]] = []
    for index in range(1, len(parts)):
        package_name = ".".join(parts[:index])
        package_path = modules_root.joinpath(*parts[:index])
        package_module = package_path.with_suffix(".py")
        package_init = package_path / "__init__.py"
        _selected_path(package_module, modules_root, package_name)
        _selected_path(package_init, modules_root, package_name)
        if _regular_file(package_module):
            raise TargetResolutionError(
                f"target parent package is ambiguous: {package_name}",
                outside=True,
            )
        if not package_path.is_dir() or package_path.is_symlink():
            raise TargetResolutionError(
                f"target parent package not found: {package_name}",
                outside=True,
            )
        if not _regular_file(package_init):
            raise TargetResolutionError(
                f"target parent package initializer not found: {package_name}",
                outside=True,
            )
        package_sources.append((package_name, package_init.resolve()))

    module_path = modules_root.joinpath(*parts)
    module_file = module_path.with_suffix(".py")
    package_init = module_path / "__init__.py"
    _selected_path(module_file, modules_root, module_name)
    _selected_path(package_init, modules_root, module_name)
    candidates = [
        (module_file, False),
        (package_init, True),
    ]
    existing = [(path, is_package) for path, is_package in candidates if _regular_file(path)]
    if len(existing) > 1:
        raise TargetResolutionError(
            f"target module is ambiguous: {module_name}",
            outside=True,
        )
    if not existing:
        raise TargetResolutionError(
            f"target module not found: {module_name}",
            outside=False,
        )
    source, is_package = existing[0]
    return TargetSources(
        modules_root=modules_root,
        module_name=module_name,
        package_sources=tuple(package_sources),
        source=source.resolve(),
        is_package=is_package,
    )


def _annotation_is_int_or_none(annotation: ast.expr) -> bool:
    if isinstance(annotation, ast.Name):
        return annotation.id == "int"
    if isinstance(annotation, ast.Constant):
        return annotation.value is None
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return _annotation_is_int_or_none(annotation.left) and _annotation_is_int_or_none(
            annotation.right
        )
    if isinstance(annotation, ast.Subscript):
        value = annotation.value
        if isinstance(value, ast.Name):
            name = value.id
        elif isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
            name = f"{value.value.id}.{value.attr}"
        else:
            return False
        if name not in {"Optional", "typing.Optional", "Union", "typing.Union"}:
            return False
        slice_node = annotation.slice
        elements = slice_node.elts if isinstance(slice_node, ast.Tuple) else [slice_node]
        if name in {"Optional", "typing.Optional"} and len(elements) != 1:
            return False
        return bool(elements) and all(_annotation_is_int_or_none(item) for item in elements)
    return False


def validate_module_source(source: Path) -> None:
    """Validate that a selected module is readable Python source."""
    try:
        ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        raise ValueError("target module cannot be parsed") from exc


def _runtime_annotation_is_int_or_none(annotation: Any) -> bool:
    if annotation is int or annotation is None or annotation is type(None):
        return True
    origin = get_origin(annotation)
    if origin not in (types.UnionType, Union):
        return False
    elements = get_args(annotation)
    return bool(elements) and all(_runtime_annotation_is_int_or_none(item) for item in elements)


def validate_callable_runtime(
    target: Any,
    module_name: str,
    callable_name: str,
) -> Any:
    """Validate the imported callable against the same runtime contract."""
    if not callable(target):
        raise ValueError(  # noqa: TRY004 - contract failures share one ValueError boundary
            f"target is not callable: {module_name}:{callable_name}"
        )
    call_method = inspect.getattr_static(type(target), "__call__", None)
    if inspect.iscoroutinefunction(target) or inspect.iscoroutinefunction(call_method):
        raise ValueError(
            f"target callable must be a synchronous function: {module_name}:{callable_name}"
        )
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"target callable signature is unavailable: {module_name}:{callable_name}"
        ) from exc
    annotation = signature.return_annotation
    if annotation is not inspect.Signature.empty:
        if isinstance(annotation, str):
            try:
                parsed = ast.parse(annotation, mode="eval").body
            except SyntaxError as exc:
                raise ValueError(
                    f"target callable return annotation is invalid: "
                    f"{module_name}:{callable_name}"
                ) from exc
            if not _annotation_is_int_or_none(parsed):
                raise ValueError(
                    f"target callable return annotation must be int or None: "
                    f"{module_name}:{callable_name}"
                )
        elif not _runtime_annotation_is_int_or_none(annotation):
            raise ValueError(
                f"target callable return annotation must be int or None: "
                f"{module_name}:{callable_name}"
            )
    positional = tuple(
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind
        in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
    )
    if not positional:
        raise ValueError(
            f"target callable must accept argv: {module_name}:{callable_name}"
        )
    required_positional = sum(
        parameter.default is inspect.Parameter.empty for parameter in positional
    )
    if required_positional > 1:
        raise ValueError(
            "target callable has required positional arguments beyond argv: "
            f"{module_name}:{callable_name}"
        )
    if any(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        and parameter.default is inspect.Parameter.empty
        for parameter in signature.parameters.values()
    ):
        raise ValueError(
            "target callable has required keyword-only arguments: "
            f"{module_name}:{callable_name}"
        )
    try:
        signature.bind([])
    except TypeError as exc:
        raise ValueError(
            f"target callable must accept argv: {module_name}:{callable_name}"
        ) from exc
    return target


def validate_selected_module(module: ModuleType, sources: TargetSources) -> bool:
    """Return whether an imported module came from the resolved selected source."""
    origin = getattr(module, "__file__", None)
    if not isinstance(origin, str):
        return False
    try:
        return Path(origin).resolve() == sources.source
    except OSError:
        return False
