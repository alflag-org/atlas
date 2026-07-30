"""Systemd validation, diff, atomic installation, and removal."""

from __future__ import annotations

from difflib import unified_diff
import os
from pathlib import Path
import re
import subprocess
import tempfile

from ..catalog import ServiceRef
from ..job_instances import load_job_instance


_EXEC_START_RE = re.compile(r"^ExecStart=/\S*/bin/atlas (?:job|run)(?:\s+.*)?$")
_INSTANCE_EXEC_START_RE = re.compile(
    r"^ExecStart=/\S*/bin/atlas job instance run "
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
        if self.destination_root.exists() and (
            not self.destination_root.is_dir() or self.destination_root.is_symlink()
        ):
            raise ValueError(
                f"systemd destination root must be a directory, not a symlink: "
                f"{self.destination_root}"
            )

    def validate(self, service: ServiceRef) -> None:
        """Perform structural checks and enforce a stable Atlas ExecStart."""
        for source, destination in self._artifacts(service):
            if not source.is_file() or source.is_symlink():
                raise ValueError(f"systemd source artifact not found: {source}")
            text = source.read_text(encoding="utf-8")
            if "\x00" in text or "[Unit]" not in text:
                raise ValueError(f"invalid systemd unit: {source}")
            if destination.suffix == ".service":
                if "[Service]" not in text:
                    raise ValueError(f"systemd service lacks [Service]: {source}")
                exec_start = [line.strip() for line in text.splitlines() if line.strip().startswith("ExecStart=")]
                if not exec_start or any(not _EXEC_START_RE.fullmatch(line) for line in exec_start):
                    raise ValueError(
                        "systemd service ExecStart must use the stable Atlas launcher: "
                        f"{source}"
                    )
                self._validate_job_instance(service, text, exec_start)
                if "/releases/" in text:
                    raise ValueError(f"systemd service contains a versioned release path: {source}")
            else:
                if "[Timer]" not in text:
                    raise ValueError(f"systemd timer lacks [Timer]: {source}")
                expected = f"Unit={self._unit_name(service, 'service')}"
                if expected not in text.splitlines():
                    raise ValueError(f"systemd timer must reference {expected}: {source}")

    def _validate_job_instance(
        self,
        service: ServiceRef,
        text: str,
        exec_start: list[str],
    ) -> None:
        users = [
            line.strip().removeprefix("User=")
            for line in text.splitlines()
            if line.strip().startswith("User=")
        ]
        for line in exec_start:
            match = _INSTANCE_EXEC_START_RE.fullmatch(line)
            if match is None:
                continue
            instance = load_job_instance(self.jobs_dir, match.group("instance"))
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
            if destination.is_symlink():
                raise ValueError(f"systemd destination must not be a symlink: {destination}")
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
                os.fsync(handle.fileno())
            temporary.chmod(0o644)
            os.chown(temporary, 0, 0)
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
            if destination.is_symlink():
                raise ValueError(f"systemd destination must not be a symlink: {destination}")
            self._atomic_install(source, destination)
            destinations.append(destination)
        self.reload()
        return destinations

    def remove(self, service: ServiceRef) -> list[Path]:
        """Remove known unit names only, then reload when anything changed."""
        self._validate_destination_root()
        removed: list[Path] = []
        for _, destination in self._artifacts(service):
            if destination.is_symlink():
                raise ValueError(f"systemd destination must not be a symlink: {destination}")
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
