"""Direct systemd operations; no application imports or process supervision."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .config import DaemonInstance, trusted_file

_MARKER = "# Managed by atlas daemon\n"


def _quote(value: str, *, command: bool = False) -> str:
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if command:
        value = value.replace("$", "$$")
    return f'"{value}"'


def render_unit(instance: DaemonInstance) -> str:
    command = " ".join(
        _quote(value, command=True)
        for value in (instance.executable, *instance.arguments)
    )
    lines = [
        _MARKER.rstrip(),
        "[Unit]",
        f"Description=Atlas daemon {instance.name}",
        "",
        "[Service]",
        f"Type={'notify' if instance.notify else 'exec'}",
        f"User={instance.user}",
        "WorkingDirectory=" + instance.working_directory.replace("%", "%%"),
        f"ExecStart={command}",
        f"StateDirectory=atlas/{instance.name}",
        "StateDirectoryMode=0700",
        f"RuntimeDirectory=atlas/{instance.name}",
        "RuntimeDirectoryMode=0700",
        "UMask=0077",
        "KillMode=control-group",
        f"Restart={instance.restart}",
        f"RestartSec={instance.restart_delay_seconds}",
        f"TimeoutStartSec={instance.startup_timeout_seconds}",
        f"TimeoutStopSec={instance.stop_timeout_seconds}",
        "StandardOutput=journal",
        "StandardError=journal",
    ]
    if instance.notify:
        lines.append("NotifyAccess=main")
    lines.extend(
        f"EnvironmentFile={_quote(path)}" for path in instance.environment_files
    )
    return "\n".join([*lines, "", "[Install]", "WantedBy=multi-user.target", ""])


class Systemd:
    def __init__(self, unit_dir: Path = Path("/etc/systemd/system")) -> None:
        self.unit_dir = unit_dir

    def _systemctl(self, *args: str) -> str:
        result = subprocess.run(
            ["systemctl", "--no-ask-password", *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            # Provider diagnostics can include environment values or application output.
            raise RuntimeError(f"systemctl {args[0]} failed (exit {result.returncode})")
        return result.stdout

    def status(self, instance: DaemonInstance) -> dict:
        output = self._systemctl(
            "show",
            instance.unit,
            "--property=LoadState,ActiveState,SubState,UnitFileState,Type",
        )
        values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        active = values.get("ActiveState", "unknown")
        ready = active == "active" if values.get("Type") == "notify" else None
        return {
            "schema": "atlas.daemon-status/v1",
            "name": instance.name,
            "installed": values.get("LoadState") == "loaded",
            "enabled": values.get("UnitFileState", "unknown"),
            "active": active,
            "ready": ready,
            "health": "unknown",
        }

    def action(self, instance: DaemonInstance, action: str) -> None:
        if action not in {"enable", "disable", "start", "stop", "restart"}:
            raise ValueError("unsupported systemd action")
        self._systemctl(action, instance.unit)

    def logs(self, instance: DaemonInstance, since: str | None) -> int:
        args = ["journalctl", "--no-pager", "--unit", instance.unit]
        if since is not None:
            args.extend(["--since", since])
        return subprocess.run(args, check=False).returncode

    @contextmanager
    def _locked_unit(self, instance: DaemonInstance):
        path = self.unit_dir / instance.unit
        # Validate ancestors before opening any files in the privileged unit directory.
        lock = self.unit_dir / ".atlas-daemon.lock"
        for parent in (self.unit_dir, *self.unit_dir.parents):
            info = parent.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid not in (0, os.geteuid())
                or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)
            ):
                raise ValueError("systemd unit directory must be owner controlled")
        fd = os.open(
            lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
        )
        try:
            trusted_file(lock)
            fcntl.flock(fd, fcntl.LOCK_EX)
            if path.exists() or path.is_symlink():
                trusted_file(path)
                previous = path.read_text()
                if not previous.startswith(_MARKER):
                    raise ValueError("refusing to replace an unmanaged unit")
            else:
                previous = None
            yield path, previous
        finally:
            os.close(fd)

    def _replace(self, path: Path, content: str | None) -> None:
        if content is None:
            path.unlink(missing_ok=True)
            return
        fd, temporary = tempfile.mkstemp(prefix=".atlas-unit-", dir=self.unit_dir)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(content)
                stream.flush()
                os.fchmod(stream.fileno(), 0o644)
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def install(self, instance: DaemonInstance, *, remove: bool = False) -> None:
        with self._locked_unit(instance) as (path, previous):
            if remove:
                status = self.status(instance)
                if status["active"] not in {"inactive", "failed"}:
                    raise ValueError("stop the daemon before removing its unit")
                if status["enabled"] not in {"disabled", "static", "not-found"}:
                    raise ValueError("disable the daemon before removing its unit")
            self._replace(path, None if remove else render_unit(instance))
            try:
                self._systemctl("daemon-reload")
            except (OSError, RuntimeError):
                self._replace(path, previous)
                self._systemctl("daemon-reload")
                raise


def audit(instance: DaemonInstance, action: str) -> None:
    """Record successful operator requests in the system journal, separate from runs."""
    import syslog

    syslog.openlog("atlas-daemon")
    syslog.syslog(
        syslog.LOG_NOTICE,
        json.dumps(
            {
                "schema": "atlas.daemon-event/v1",
                "instance": instance.name,
                "action": action,
                "uid": os.getuid(),
            },
            sort_keys=True,
        ),
    )
