from __future__ import annotations

from pathlib import Path
import os

from .models import SECRETS_SCHEMA, load_yaml_config


def _validate_target_path(target_raw: str, allowed_root: Path) -> Path:
    target = Path(target_raw).expanduser().resolve()
    root = allowed_root.resolve()
    if not str(target).startswith(str(root) + os.sep):
        raise ValueError(f"secret target is outside allowed root: {target}")
    if target.exists() and target.is_symlink():
        raise ValueError(f"secret target cannot be a symlink: {target}")
    cur = target.parent
    while cur != root:
        if cur.is_symlink():
            raise ValueError(f"secret target parent cannot be a symlink: {cur}")
        cur = cur.parent
    return target


def materialize_secrets(etc_dir: Path) -> tuple[list[Path], list[str]]:
    spec = etc_dir / "secrets.yml"
    if not spec.exists():
        return [], []
    data = load_yaml_config(spec, SECRETS_SCHEMA)
    secret_items = data.get("secrets", [])
    written: list[Path] = []
    secret_values: list[str] = []
    allowed_root = (etc_dir / "secrets").resolve()
    for item in secret_items:
        target = _validate_target_path(str(item["target"]), allowed_root)
        env_key = str(item.get("env", ""))
        if not env_key:
            raise ValueError(f"missing env for secret target {target}")
        value = os.environ.get(env_key)
        if value is None:
            raise ValueError(f"secret env not found: {env_key}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value)
        mode = int(str(item.get("mode", "0600")), 8)
        target.chmod(mode)
        written.append(target)
        secret_values.append(value)
    return written, secret_values
