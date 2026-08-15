from __future__ import annotations

import stat
import sys
from pathlib import Path


def configure_environment(monkeypatch, tmp_path: Path, config: str, host: str | None = None) -> tuple[Path, Path, Path]:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True)
    (etc / "config.yml").write_text(config, encoding="utf-8")
    (etc / "host.yml").write_text(
        host
        or "version: 1\nhost:\n  id: test-host\n  role: test\n  site: local\n",
        encoding="utf-8",
    )
    for key, value in {
        "ATLAS_HOME": home,
        "ATLAS_ETC_DIR": etc,
        "ATLAS_VAR_DIR": var,
        "ATLAS_RUNTIMES_DIR": home / "runtimes",
        "ATLAS_VENVS_DIR": home / "venvs",
        "ATLAS_SHIMS_DIR": home / "shims",
    }.items():
        monkeypatch.setenv(key, str(value))
    return home, etc, var


def python_config(root: Path, *, executable: Path | None = None, program_name: str = "sample") -> str:
    executable_line = ""
    if executable is not None:
        executable_line = f"    executable: {executable}\n"
    return (
        "runtime:\n"
        "  python:\n"
        f"    version: '{sys.version_info.major}.{sys.version_info.minor}'\n"
        f"{executable_line}"
        "programs:\n"
        f"  {program_name}:\n"
        f"    root: {root}\n"
        "    runtime:\n"
        "      type: python\n"
        f"      venv: {program_name}\n"
    )


def write_python_command(root: Path, name: str = "sample", body: str | None = None) -> Path:
    path = root / "commands" / f"{name}.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        body
        or "from atlas_core import get_context\n"
        "import json, os, sys\n"
        "ctx = get_context()\n"
        "payload = {'host': ctx.host.id, 'program': ctx.program.name, 'command': ctx.command.name, 'args': sys.argv[1:], 'context': os.environ['ATLAS_CONTEXT_FILE']}\n"
        "print(json.dumps(payload, sort_keys=True))\n",
        encoding="utf-8",
    )
    return path


def write_native_command(root: Path, name: str = "native", body: str | None = None) -> Path:
    path = root / "bin" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body or "#!/bin/sh\nprintf '%s\\n' \"$*\"\n", encoding="utf-8")
    path.chmod(
        stat.S_IRUSR
        | stat.S_IWUSR
        | stat.S_IXUSR
        | stat.S_IRGRP
        | stat.S_IXGRP
        | stat.S_IROTH
        | stat.S_IXOTH
    )
    return path
