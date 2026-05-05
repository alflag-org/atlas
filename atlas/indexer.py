from __future__ import annotations

from pathlib import Path

import yaml  # type: ignore[import-untyped]


_ALLOWED_HEADER_KEYS = {
    "name",
    "timeout",
    "lock",
    "allowed_roles",
    "destructive",
    "direct_exec",
}
_DEFAULT_TIMEOUT_SECONDS = 300
_MAX_TIMEOUT_SECONDS = 86400


def _parse_bool(value: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError("boolean must be 'true' or 'false'")


def _parse_int(value: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError("integer required") from exc


def _parse_list(value: str) -> list[str]:
    items = [r.strip() for r in value.split(",") if r.strip()]
    return items


def _parse_header_metadata(cmd: Path) -> dict[str, object]:
    meta: dict[str, object] = {}
    for line in cmd.read_text().splitlines()[:30]:
        s = line.strip()
        if not s.startswith("# atlas:"):
            continue
        payload = s[len("# atlas:") :].strip()
        if "=" not in payload:
            raise ValueError(f"invalid header metadata for {cmd}: {payload}")
        key, raw = [p.strip() for p in payload.split("=", 1)]
        if key not in _ALLOWED_HEADER_KEYS:
            raise ValueError(f"unsupported header metadata key for {cmd}: {key}")
        if key in meta:
            raise ValueError(f"duplicated header metadata key for {cmd}: {key}")
        if key == "name":
            if not raw:
                raise ValueError(
                    f"invalid header metadata for {cmd}: name must not be empty"
                )
            meta[key] = raw
        elif key == "allowed_roles":
            try:
                meta[key] = _parse_list(raw)
            except ValueError as exc:
                raise ValueError(
                    f"invalid header metadata type for {cmd}: allowed_roles must be list[str]"
                ) from exc
        elif key == "destructive":
            try:
                meta[key] = _parse_bool(raw.lower())
            except ValueError as exc:
                raise ValueError(
                    f"invalid header metadata type for {cmd}: destructive must be bool"
                ) from exc
        elif key == "direct_exec":
            try:
                meta[key] = _parse_bool(raw.lower())
            except ValueError as exc:
                raise ValueError(
                    f"invalid header metadata type for {cmd}: direct_exec must be bool"
                ) from exc
        elif key == "timeout":
            try:
                timeout = _parse_int(raw)
            except ValueError as exc:
                raise ValueError(
                    f"invalid header metadata type for {cmd}: timeout must be int"
                ) from exc
            if timeout < 1 or timeout > _MAX_TIMEOUT_SECONDS:
                raise ValueError(f"timeout header out of range for {cmd}: {timeout}")
            meta["timeout_sec"] = timeout
        elif key == "lock":
            if not raw:
                raise ValueError(
                    f"invalid header metadata for {cmd}: lock must not be empty"
                )
            meta[key] = raw
    return meta


def discover_commands(release_dir: Path) -> dict[str, dict[str, dict[str, object]]]:
    result: dict[str, dict[str, object]] = {}
    packs_dir = release_dir / "packs"
    if not packs_dir.exists():
        return {"commands": result}
    for pack in packs_dir.iterdir():
        bindir = pack / "bin"
        if not bindir.exists():
            continue
        for cmd in bindir.iterdir():
            if cmd.is_file() and cmd.stat().st_mode & 0o111:
                meta: dict[str, object] = {
                    "path": str(cmd.relative_to(release_dir)),
                    "pack": pack.name,
                    "name": cmd.name,
                    "timeout_sec": _DEFAULT_TIMEOUT_SECONDS,
                    "lock": cmd.name,
                    "destructive": False,
                    "direct_exec": False,
                    "allowed_roles": [],
                    "enabled": True,
                }
                meta.update(_parse_header_metadata(cmd))
                name = str(meta["name"])
                if name in result:
                    raise ValueError(f"duplicated command name: {name}")
                result[name] = meta
    return {"commands": result}


def write_command_index(release_dir: Path) -> Path:
    data = discover_commands(release_dir)
    target = release_dir / "command-index.yml"
    target.write_text(yaml.safe_dump(data, sort_keys=False))
    return target
