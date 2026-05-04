from __future__ import annotations

from pathlib import Path
import json


_ALLOWED_HEADER_KEYS = {"roles", "destructive", "timeout"}
_MAX_TIMEOUT_SECONDS = 3600


def _parse_bool(value: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError("boolean must be 'true' or 'false'")


def _parse_header_metadata(cmd: Path) -> dict[str, object]:
    meta: dict[str, object] = {}
    for line in cmd.read_text().splitlines()[:30]:
        s = line.strip()
        if not s.startswith("# atlas:"):
            continue
        payload = s[len("# atlas:"):].strip()
        if "=" not in payload:
            raise ValueError(f"invalid header metadata for {cmd}: {payload}")
        key, raw = [p.strip() for p in payload.split("=", 1)]
        if key not in _ALLOWED_HEADER_KEYS:
            raise ValueError(f"unsupported header metadata key for {cmd}: {key}")
        if key in meta:
            raise ValueError(f"duplicated header metadata key for {cmd}: {key}")
        if key == "roles":
            roles = [r.strip() for r in raw.split(",") if r.strip()]
            if not roles:
                raise ValueError(f"roles header must contain at least one role for {cmd}")
            meta[key] = roles
        elif key == "destructive":
            meta[key] = _parse_bool(raw.lower())
        elif key == "timeout":
            timeout = int(raw)
            if timeout < 1 or timeout > _MAX_TIMEOUT_SECONDS:
                raise ValueError(f"timeout header out of range for {cmd}: {timeout}")
            meta[key] = timeout
    return meta


def discover_commands(release_dir: Path) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    packs_dir = release_dir / "packs"
    if not packs_dir.exists():
        return result
    for pack in packs_dir.iterdir():
        bindir = pack / "bin"
        if not bindir.exists():
            continue
        for cmd in bindir.iterdir():
            if cmd.is_file() and cmd.stat().st_mode & 0o111:
                result[cmd.name] = {
                    "path": str(cmd.relative_to(release_dir)),
                    "pack": pack.name,
                    "roles": [],
                    "destructive": False,
                }
                result[cmd.name].update(_parse_header_metadata(cmd))
    return result


def write_command_index(release_dir: Path) -> Path:
    data = discover_commands(release_dir)
    target = release_dir / "command-index.json"
    target.write_text(json.dumps(data, indent=2))
    return target
