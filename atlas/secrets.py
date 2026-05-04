from __future__ import annotations

from pathlib import Path
import os

from .models import SECRETS_SCHEMA, load_yaml_config


def materialize_secrets(etc_dir: Path) -> list[Path]:
    spec = etc_dir / "secrets.yml"
    if not spec.exists():
        return []
    data = load_yaml_config(spec, SECRETS_SCHEMA)
    secret_items = data.get("secrets", [])
    written: list[Path] = []
    for item in secret_items:
        target = Path(item["target"])
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
    return written
