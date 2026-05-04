from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json


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
        # list block
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


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Node:
    name: str = "unknown"
    role: str = ""
    packs: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "Node":
        if not path.exists():
            return cls()
        data = parse_yaml_like(path.read_text())
        return cls(name=data.get("name", "unknown"), role=data.get("role", ""), packs=list(data.get("packs", [])))


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
