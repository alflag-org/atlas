"""Filesystem locations owned by Atlas."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AtlasPaths:
    """Resolved Atlas paths for the host and child processes."""

    home: Path
    etc: Path
    var: Path
    runtimes: Path
    python_runtimes: Path
    venvs: Path
    shims: Path
    launchers: Path
    logs: Path
    runtime_state: Path
    context_dir: Path
    config_file: Path
    host_file: Path
    run_log: Path

    def to_dict(self) -> dict[str, str]:
        """Return the paths in a JSON-friendly form."""
        return {
            "home": str(self.home),
            "etc": str(self.etc),
            "var": str(self.var),
            "runtimes": str(self.runtimes),
            "python_runtimes": str(self.python_runtimes),
            "venvs": str(self.venvs),
            "shims": str(self.shims),
            "launchers": str(self.launchers),
            "logs": str(self.logs),
            "runtime_state": str(self.runtime_state),
            "context_dir": str(self.context_dir),
            "config_file": str(self.config_file),
            "host_file": str(self.host_file),
            "run_log": str(self.run_log),
        }

    def python_runtime(self, version: str) -> Path:
        """Return Atlas's selected interpreter path for ``version``."""
        if not version or Path(version).name != version:
            raise ValueError(f"invalid Python runtime version: {version}")
        return self.python_runtimes / version / "bin" / "python"

    def venv(self, name: str) -> Path:
        """Return the dedicated venv path for a program."""
        if not name or Path(name).name != name:
            raise ValueError(f"invalid venv name: {name}")
        return self.venvs / name


def get_paths(env: Mapping[str, str] | None = None) -> AtlasPaths:
    """Resolve paths from environment variables and Atlas defaults."""
    read_env = os.environ if env is None else env
    home = Path(read_env.get("ATLAS_HOME", "/opt/atlas"))
    etc = Path(read_env.get("ATLAS_ETC_DIR", "/etc/atlas"))
    var = Path(read_env.get("ATLAS_VAR_DIR", "/var/lib/atlas"))
    runtimes = Path(read_env.get("ATLAS_RUNTIMES_DIR", str(home / "runtimes")))
    python_runtimes = runtimes / "python"
    venvs = Path(read_env.get("ATLAS_VENVS_DIR", str(home / "venvs")))
    runtime_state = var / "runtime-state"
    return AtlasPaths(
        home=home,
        etc=etc,
        var=var,
        runtimes=runtimes,
        python_runtimes=python_runtimes,
        venvs=venvs,
        shims=Path(read_env.get("ATLAS_SHIMS_DIR", str(home / "shims"))),
        launchers=Path(read_env.get("ATLAS_LAUNCHERS_DIR", str(home / "launchers"))),
        logs=var / "logs",
        runtime_state=runtime_state,
        context_dir=runtime_state / "contexts",
        config_file=Path(read_env.get("ATLAS_CONFIG_FILE", str(etc / "config.yml"))),
        host_file=Path(read_env.get("ATLAS_HOST_FILE", str(etc / "host.yml"))),
        run_log=var / "logs" / "runs.jsonl",
    )


def ensure_dirs(paths: AtlasPaths) -> None:
    """Create Atlas-owned writable directories."""
    for path in (
        paths.home,
        paths.etc,
        paths.var,
        paths.runtimes,
        paths.python_runtimes,
        paths.venvs,
        paths.shims,
        paths.launchers,
        paths.logs,
        paths.runtime_state,
        paths.context_dir,
    ):
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ValueError(f"Atlas path must be a directory: {path}")
        path.mkdir(parents=True, exist_ok=True)
