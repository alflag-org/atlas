"""Pure target resolution and callable-contract checks for release code."""

from __future__ import annotations

import ast
import inspect
import re
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

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


def _bound_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, (ast.Tuple, ast.List)):
        return [name for item in node.elts for name in _bound_names(item)]
    if isinstance(node, ast.Starred):
        return _bound_names(node.value)
    return []


def _module_bindings(tree: ast.Module) -> list[tuple[str, str, ast.AST]]:
    bindings: list[tuple[str, str, ast.AST]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            kind = "async" if isinstance(node, ast.AsyncFunctionDef) else "function"
            if isinstance(node, ast.ClassDef):
                kind = "other"
            bindings.append((node.name, kind, node))
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                bindings.extend((name, "rebind", node) for name in _bound_names(target))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            aliases = node.names
            for alias in aliases:
                name = alias.asname or alias.name.split(".")[0]
                bindings.append((name, "rebind", node))
        elif isinstance(node, ast.Delete):
            for target in node.targets:
                bindings.extend((name, "rebind", node) for name in _bound_names(target))
    return bindings


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
        return bool(elements) and all(_annotation_is_int_or_none(item) for item in elements)
    return False


def validate_callable_source(source: Path, callable_name: str) -> None:
    """Validate one target's static callable contract without importing it."""
    try:
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        raise ValueError("target module cannot be parsed") from exc
    matches = [binding for binding in _module_bindings(tree) if binding[0] == callable_name]
    if any(kind == "async" for _, kind, _ in matches):
        raise ValueError(
            f"target callable must be a synchronous function: {callable_name}"
        )
    if not matches or any(kind != "function" for _, kind, _ in matches) or len(matches) != 1:
        raise ValueError(f"target callable is not a function: {callable_name}")
    function = matches[0][2]
    assert isinstance(function, ast.FunctionDef)
    positional = [*function.args.posonlyargs, *function.args.args]
    if not positional:
        raise ValueError(f"target callable must accept argv: {callable_name}")
    required_positional = len(positional) - len(function.args.defaults)
    if required_positional > 1:
        raise ValueError(
            f"target callable has required positional arguments beyond argv: {callable_name}"
        )
    if any(default is None for default in function.args.kw_defaults):
        raise ValueError(
            f"target callable has required keyword-only arguments: {callable_name}"
        )
    if function.returns is not None and not _annotation_is_int_or_none(function.returns):
        raise ValueError(
            f"target callable return annotation must be int or None: {callable_name}"
        )


def validate_callable_runtime(
    target: Any,
    module_name: str,
    callable_name: str,
) -> None:
    """Validate the imported callable against the same runtime contract."""
    if not inspect.isfunction(target):
        raise ValueError(f"target is not a function: {module_name}:{callable_name}")
    if inspect.iscoroutinefunction(target):
        raise ValueError(
            f"target callable must be a synchronous function: {module_name}:{callable_name}"
        )
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"target callable signature is unavailable: {module_name}:{callable_name}"
        ) from exc
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
    try:
        signature.bind([])
    except TypeError as exc:
        raise ValueError(
            f"target callable must accept argv: {module_name}:{callable_name}"
        ) from exc


def validate_selected_module(module: ModuleType, sources: TargetSources) -> bool:
    """Return whether an imported module came from the resolved selected source."""
    origin = getattr(module, "__file__", None)
    if not isinstance(origin, str):
        return False
    try:
        return Path(origin).resolve() == sources.source
    except OSError:
        return False
