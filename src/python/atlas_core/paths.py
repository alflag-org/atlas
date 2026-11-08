"""Atlas paths exposed to automation programs."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AtlasPaths:
    """Standard Atlas directories available to a child process."""

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
        """Return a JSON-friendly representation."""
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


def get_paths(env: Mapping[str, str] | None = None) -> AtlasPaths:
    """Resolve standard paths from the child environment."""
    read_env = os.environ if env is None else env
    home = Path(read_env.get("ATLAS_HOME", "/opt/atlas"))
    etc = Path(read_env.get("ATLAS_ETC_DIR", "/etc/atlas"))
    var = Path(read_env.get("ATLAS_VAR_DIR", "/var/lib/atlas"))
    runtimes = Path(read_env.get("ATLAS_RUNTIMES_DIR", str(home / "runtimes")))
    runtime_state = var / "runtime-state"
    return AtlasPaths(
        home=home,
        etc=etc,
        var=var,
        runtimes=runtimes,
        python_runtimes=runtimes / "python",
        venvs=Path(read_env.get("ATLAS_VENVS_DIR", str(home / "venvs"))),
        shims=Path(read_env.get("ATLAS_SHIMS_DIR", str(home / "shims"))),
        launchers=Path(read_env.get("ATLAS_LAUNCHERS_DIR", str(home / "launchers"))),
        logs=var / "logs",
        runtime_state=runtime_state,
        context_dir=runtime_state / "contexts",
        config_file=Path(read_env.get("ATLAS_CONFIG_FILE", str(etc / "config.yml"))),
        host_file=Path(read_env.get("ATLAS_HOST_FILE", str(etc / "host.yml"))),
        run_log=var / "logs" / "runs.jsonl",
    )
