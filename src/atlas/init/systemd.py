"""Systemd validation, diff, atomic installation, and removal."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from difflib import unified_diff
from pathlib import Path

from ..catalog import ServiceRef
from ..job_instances import load_job_instance

_INSTANCE_EXEC_START_RE = re.compile(
    r"^ExecStart=/opt/atlas/bin/atlas job instance run "
    r"(?P<instance>[a-z][a-z0-9]*(?:-[a-z0-9]+)*)$"
)


class SystemdAdapter:
    """Manage only Atlas-owned systemd artifacts."""

    def __init__(
        self,
        destination_root: Path = Path("/etc/systemd/system"),
        systemctl: str = "systemctl",
        *,
        jobs_dir: Path = Path("/etc/atlas/jobs.d"),
    ) -> None:
        if not destination_root.is_absolute():
            raise ValueError("systemd destination root must be absolute")
        if not jobs_dir.is_absolute():
            raise ValueError("jobs directory must be absolute")
        self.destination_root = destination_root
        self.systemctl = systemctl
        self.jobs_dir = jobs_dir

    @staticmethod
    def _unit_name(service: ServiceRef, suffix: str) -> str:
        return f"atlas-{service.release.name}-{service.service.name}.{suffix}"

    def _artifacts(self, service: ServiceRef) -> list[tuple[Path, Path]]:
        artifacts = [
            (
                service.service.systemd.service,
                self.destination_root / self._unit_name(service, "service"),
            )
        ]
        if service.service.systemd.timer is not None:
            artifacts.append(
                (
                    service.service.systemd.timer,
                    self.destination_root / self._unit_name(service, "timer"),
                )
            )
        return artifacts

    def _validate_destination_root(self) -> None:
        if self.destination_root.is_symlink() or (
            self.destination_root.exists() and not self.destination_root.is_dir()
        ):
            raise ValueError(
                "systemd destination root must be a directory and must not be a symlink: "
                f"{self.destination_root}"
            )

    @staticmethod
    def _validate_destination(destination: Path) -> None:
        if destination.is_symlink():
            raise ValueError(
                f"systemd destination must not be a symlink: {destination}"
            )
        if destination.exists() and not destination.is_file():
            raise ValueError(
                f"systemd destination must be a regular file: {destination}"
            )

    def validate(self, service: ServiceRef) -> None:
        """Perform structural checks and enforce an Atlas host ExecStart."""
        for source, destination in self._artifacts(service):
            if not source.is_file() or source.is_symlink():
                raise ValueError(f"systemd source artifact not found: {source}")
            text = source.read_text(encoding="utf-8")
            lines = [line.strip() for line in text.splitlines()]
            if "\x00" in text or "[Unit]" not in lines:
                raise ValueError(f"invalid systemd unit: {source}")
            if destination.suffix == ".service":
                if "[Service]" not in lines:
                    raise ValueError(f"systemd service lacks [Service]: {source}")
                exec_start = [
                    line for line in lines if line.startswith("ExecStart=")
                ]
                if len(exec_start) != 1:
                    raise ValueError(
                        "systemd service must have exactly one ExecStart using the "
                        f"stable Atlas launcher: {source}"
                    )
                self._validate_exec_start(service, text, exec_start[0], source)
                if "/releases/" in text:
                    raise ValueError(
                        f"systemd service contains a versioned release path: {source}"
                    )
            else:
                if "[Timer]" not in lines:
                    raise ValueError(f"systemd timer lacks [Timer]: {source}")
                expected = f"Unit={self._unit_name(service, 'service')}"
                if expected not in lines:
                    raise ValueError(
                        f"systemd timer must reference {expected}: {source}"
                    )

    def _validate_exec_start(
        self,
        service: ServiceRef,
        text: str,
        exec_start: str,
        source: Path,
    ) -> None:
        if service.service.command is not None:
            expected = f"ExecStart=/opt/atlas/bin/atlas run {service.service.command}"
            if exec_start == expected or exec_start.startswith(expected + " "):
                return
            raise ValueError(
                "systemd service ExecStart must use the stable Atlas launcher for "
                f"declared command {service.service.command}: {source}"
            )

        match = _INSTANCE_EXEC_START_RE.fullmatch(exec_start)
        if match is None:
            raise ValueError(
                "systemd job service ExecStart must use the stable Atlas launcher "
                "through a matching job instance for "
                f"{service.release.name}/{service.service.job}: {source}"
            )
        self._validate_job_instance(service, text, match.group("instance"))

    def _validate_job_instance(
        self,
        service: ServiceRef,
        text: str,
        instance_name: str,
    ) -> None:
        users = [
            line.strip().removeprefix("User=")
            for line in text.splitlines()
            if line.strip().startswith("User=")
        ]
        instance = load_job_instance(self.jobs_dir, instance_name)
        if (
            service.service.job is None
            or instance.release != service.release.name
            or instance.job != service.service.job
        ):
            raise ValueError(
                "systemd job instance must reference the service release and job: "
                f"{instance.name}"
            )
        if users != [instance.user]:
            raise ValueError(
                "systemd service User must match the job instance user: "
                f"{instance.user}"
            )

    def diff(self, service: ServiceRef) -> str:
        """Return unified diffs for every source/destination pair."""
        self.validate(service)
        self._validate_destination_root()
        chunks: list[str] = []
        for source, destination in self._artifacts(service):
            self._validate_destination(destination)
            source_lines = source.read_text(encoding="utf-8").splitlines(keepends=True)
            destination_lines = (
                destination.read_text(encoding="utf-8").splitlines(keepends=True)
                if destination.exists()
                else []
            )
            chunks.extend(
                unified_diff(
                    destination_lines,
                    source_lines,
                    fromfile=str(destination),
                    tofile=str(source),
                )
            )
        return "".join(chunks)

    @staticmethod
    def _atomic_install(source: Path, destination: Path) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            dir=destination.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(source.read_bytes())
                handle.flush()
                os.fchmod(handle.fileno(), 0o644)
                os.fchown(handle.fileno(), 0, 0)
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()

    def install(self, service: ServiceRef) -> list[Path]:
        """Atomically install units, then run ``systemctl daemon-reload``."""
        self.validate(service)
        self._validate_destination_root()
        self.destination_root.mkdir(parents=True, exist_ok=True)
        destinations: list[Path] = []
        for source, destination in self._artifacts(service):
            self._validate_destination(destination)
            self._atomic_install(source, destination)
            destinations.append(destination)
        self.reload()
        return destinations

    def remove(self, service: ServiceRef) -> list[Path]:
        """Remove known unit names only, then reload when anything changed."""
        self._validate_destination_root()
        removed: list[Path] = []
        for _, destination in self._artifacts(service):
            self._validate_destination(destination)
            if destination.exists():
                destination.unlink()
                removed.append(destination)
        if removed:
            self.reload()
        return removed

    def reload(self) -> None:
        """Run only systemd's definition reload operation."""
        try:
            subprocess.run([self.systemctl, "daemon-reload"], check=True)
        except FileNotFoundError as exc:
            raise ValueError(f"systemctl command not found: {self.systemctl}") from exc
        except subprocess.CalledProcessError as exc:
            raise ValueError("systemctl daemon-reload failed") from exc
