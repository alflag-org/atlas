from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json

from .config import load_compat_config


class SchemaError(ValueError):
    pass


@dataclass(frozen=True)
class FieldSchema:
    typ: type | tuple[type, ...]
    required: bool = True
    default: Any = None
    allowed: set[Any] | None = None


@dataclass(frozen=True)
class ModelSchema:
    name: str
    fields: dict[str, FieldSchema]
    allow_unknown: bool = False


def _err(file_name: str, key: str, message: str) -> SchemaError:
    return SchemaError(f"{file_name}: invalid key '{key}': {message}")


def validate_config(data: dict[str, Any], schema: ModelSchema, file_name: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in data:
        if key not in schema.fields and not schema.allow_unknown:
            raise _err(file_name, key, "unknown key")

    for key, field in schema.fields.items():
        if key not in data:
            if field.required:
                raise _err(file_name, key, "missing required key")
            out[key] = field.default
            continue
        value = data[key]
        if not isinstance(value, field.typ):
            expected = field.typ if isinstance(field.typ, tuple) else (field.typ,)
            expected_name = "|".join(t.__name__ for t in expected)
            raise _err(file_name, key, f"expected type {expected_name}, got {type(value).__name__}")
        if field.allowed is not None and value not in field.allowed:
            allowed = ", ".join(sorted(str(v) for v in field.allowed))
            raise _err(file_name, key, f"unexpected value '{value}', allowed: [{allowed}]")
        out[key] = value
    return out


def parse_yaml_like(content: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    lines = [ln.rstrip("\n") for ln in content.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    i = 0
    while i < len(lines):
        line = lines[i]
        if ":" not in line:
            i += 1
            continue
        k, v = line.split(":", 1)
        key = k.strip()
        v = v.strip()
        if v:
            data[key] = v
            i += 1
            continue
        items: list[Any] = []
        i += 1
        while i < len(lines) and lines[i].startswith("  - "):
            item_line = lines[i][4:]
            if ":" in item_line:
                obj: dict[str, Any] = {}
                k2, v2 = item_line.split(":", 1)
                obj[k2.strip()] = v2.strip().strip('"')
                i += 1
                while i < len(lines) and lines[i].startswith("    ") and ":" in lines[i]:
                    k3, v3 = lines[i].strip().split(":", 1)
                    obj[k3.strip()] = v3.strip().strip('"')
                    i += 1
                items.append(obj)
            else:
                items.append(item_line.strip())
                i += 1
        data[key] = items
    return data


NODE_SCHEMA = ModelSchema(
    name="node.yml",
    fields={
        "name": FieldSchema(str, required=False, default="unknown"),
        "role": FieldSchema(str, required=False, default=""),
        "packs": FieldSchema(list, required=False, default=[]),
    },
)

MANIFEST_SCHEMA = ModelSchema(
    name="manifest.yml",
    fields={
        "payload": FieldSchema(str, required=True),
        "checksum": FieldSchema(str, required=False, default=""),
    },
)

COMMAND_INDEX_SCHEMA = ModelSchema(
    name="command-index.yml",
    fields={
        "commands": FieldSchema(list, required=False, default=[]),
    },
)

PACK_SCHEMA = ModelSchema(
    name="pack.yml",
    fields={
        "name": FieldSchema(str, required=True),
        "version": FieldSchema(str, required=False, default=""),
        "enabled": FieldSchema(str, required=False, default="true", allowed={"true", "false"}),
    },
)

SECRETS_SCHEMA = ModelSchema(
    name="secrets.yml",
    fields={
        "secrets": FieldSchema(list, required=False, default=[]),
    },
)


def load_yaml_config(path: Path, schema: ModelSchema) -> dict[str, Any]:
    data, loaded = load_compat_config(path.parent, path.stem)
    if loaded is None:
        return validate_config({}, schema, schema.name)
    return validate_config(data, schema, loaded.name)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Node:
    name: str = "unknown"
    role: str = ""
    packs: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "Node":
        data = load_yaml_config(path, NODE_SCHEMA)
        return cls(name=data["name"], role=data["role"], packs=list(data["packs"]))


@dataclass
class RuntimeState:
    node: str | None = None
    current_version: str | None = None
    previous_version: str | None = None
    last_pull_at: str | None = None
    last_apply_at: str | None = None
    last_apply_status: str | None = None
    updated_at: str = field(default_factory=utcnow)

    @classmethod
    def load(cls, path: Path) -> "RuntimeState":
        if not path.exists():
            return cls()
        return cls(**json.loads(path.read_text()))

    def save(self, path: Path) -> None:
        self.updated_at = utcnow()
        path.write_text(json.dumps(asdict(self), indent=2))
