from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from atlas_operations.operation.errors import PlanError


def read_json_stdin() -> dict[str, Any]:
    return _parse_json(sys.stdin.read(), "<stdin>")


def read_artifact_arg(arg: str | None) -> dict[str, Any]:
    if arg in (None, "-"):
        return read_json_stdin()
    path = Path(arg)
    if path.is_file() and not path.is_symlink():
        return _parse_json(path.read_text(encoding="utf-8"), str(path))
    raise PlanError(f"artifact does not exist or is unsafe: {arg}")


def write_json_stdout(data: Any) -> None:
    print(json.dumps(data, indent=2, sort_keys=False, ensure_ascii=False, default=str))


def write_diag_stderr(text: str) -> None:
    print(text, file=sys.stderr)


def _parse_json(raw: str, source: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PlanError(f"{source} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise PlanError(f"{source} must contain a JSON object")
    return data
