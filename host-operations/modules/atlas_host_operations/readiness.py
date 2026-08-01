"""Provider-neutral TCP, SSH, and cloud-init readiness checks."""

from __future__ import annotations

import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from atlas_host_operations.models import (
    CheckResult,
    ProviderObservation,
    VerificationResult,
)
from atlas_host_operations.providers.base import HostContext
from atlas_host_operations.subprocesses import CommandRunner, SubprocessRunner


class ReadinessChecker(Protocol):
    def wait(
        self,
        context: HostContext,
        observation: ProviderObservation,
    ) -> VerificationResult: ...


class HostReadinessChecker:
    def __init__(
        self,
        runner: CommandRunner | None = None,
        *,
        connector: Callable[..., object] = socket.create_connection,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        timeout_seconds: int = 900,
        poll_interval_seconds: int = 5,
    ) -> None:
        self._runner = runner or SubprocessRunner()
        self._connector = connector
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._timeout_seconds = timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds

    def wait(
        self,
        context: HostContext,
        observation: ProviderObservation,
    ) -> VerificationResult:
        checks = [
            _check("readiness.provider.exists", observation.exists),
            _check("readiness.provider.running", observation.running),
            _check(
                "readiness.provider.address",
                context.plan.readiness.address in observation.addresses,
            ),
        ]
        if context.plan.readiness.require_guest_agent:
            checks.append(
                _check("readiness.provider.guest-agent", observation.guest_agent_ready)
            )
        if any(check.status == "failed" for check in checks):
            return VerificationResult(status="failed", checks=checks)
        checks.append(self._wait_for_tcp(context))
        if checks[-1].status == "failed":
            return VerificationResult(status="failed", checks=checks)
        ssh_argv = self._ssh_argv(context, ["true"])
        ssh = self._runner.run(ssh_argv, timeout_seconds=30)
        checks.append(_check("readiness.ssh-authentication", ssh.return_code == 0))
        if context.plan.readiness.require_cloud_init and ssh.return_code == 0:
            cloud_init = self._runner.run(
                self._ssh_argv(context, ["cloud-init", "status", "--wait"]),
                timeout_seconds=300,
            )
            checks.append(_check("readiness.cloud-init", cloud_init.return_code == 0))
        status = (
            "failed" if any(check.status == "failed" for check in checks) else "passed"
        )
        return VerificationResult(status=status, checks=checks)

    def _wait_for_tcp(self, context: HostContext) -> CheckResult:
        target = (
            context.plan.readiness.address,
            context.plan.readiness.ssh_port,
        )
        deadline = self._monotonic() + self._timeout_seconds
        while True:
            try:
                connection = self._connector(target, timeout=5)
            except OSError:
                if self._monotonic() >= deadline:
                    return _check("readiness.tcp", False)
                self._sleeper(self._poll_interval_seconds)
                continue
            close = getattr(connection, "close", None)
            if callable(close):
                close()
            return _check("readiness.tcp", True)

    @staticmethod
    def _ssh_argv(context: HostContext, remote: list[str]) -> list[str]:
        return [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-p",
            str(context.plan.readiness.ssh_port),
            f"{context.plan.readiness.ssh_user}@{context.plan.readiness.address}",
            "--",
            *remote,
        ]


@dataclass
class FakeReadinessChecker:
    status: str = "passed"
    calls: list[str] = field(default_factory=list)

    def wait(
        self,
        context: HostContext,
        observation: ProviderObservation,
    ) -> VerificationResult:
        self.calls.append(context.plan.resource.id)
        passed = self.status == "passed" and observation.running
        return VerificationResult(
            status="passed" if passed else "failed",
            checks=[_check("readiness.fake", passed)],
        )


def _check(name: str, passed: bool) -> CheckResult:
    return CheckResult(name=name, status="passed" if passed else "failed")
