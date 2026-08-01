"""Ansible host configuration delegated to reviewed config commands."""

from __future__ import annotations

from pathlib import Path

from atlas_host_operations.errors import AdapterError
from atlas_host_operations.models import CheckResult, StepResult, VerificationResult
from atlas_host_operations.providers.base import HostContext
from atlas_host_operations.subprocesses import (
    ChildResult,
    CommandRunner,
    SubprocessRunner,
)


class AnsibleHostConfigurator:
    name = "ansible"

    def __init__(self, runner: CommandRunner | None = None) -> None:
        self._runner = runner or SubprocessRunner()

    def validate(self, context: HostContext) -> list[CheckResult]:
        project = _project(context)
        checks: list[CheckResult] = []
        for playbook in (
            context.plan.configuration.bootstrap_playbook,
            context.plan.configuration.converge_playbook,
        ):
            result = self._runner.run(
                ["config-validate", playbook],
                cwd=project,
                timeout_seconds=300,
            )
            checks.append(
                CheckResult(
                    name=f"configuration.ansible.{playbook}",
                    status="passed" if result.return_code == 0 else "failed",
                    message="" if result.return_code == 0 else _diagnostic(result),
                )
            )
        return checks

    def bootstrap(self, context: HostContext) -> StepResult:
        return self._apply(
            context,
            context.plan.configuration.bootstrap_playbook,
            timeout_seconds=1800,
        )

    def converge(self, context: HostContext) -> StepResult:
        return self._apply(
            context,
            context.plan.configuration.converge_playbook,
            timeout_seconds=3600,
        )

    def verify(self, context: HostContext) -> VerificationResult:
        result = self._runner.run(
            [
                "config-check",
                context.plan.configuration.converge_playbook,
                context.plan.configuration.target,
            ],
            cwd=_project(context),
            timeout_seconds=1800,
        )
        passed = result.return_code == 0
        return VerificationResult(
            status="passed" if passed else "failed",
            checks=[
                CheckResult(
                    name="configuration.ansible.check",
                    status="passed" if passed else "failed",
                    message="" if passed else _diagnostic(result),
                )
            ],
        )

    def _apply(
        self,
        context: HostContext,
        playbook: str,
        *,
        timeout_seconds: int,
    ) -> StepResult:
        result = self._runner.run(
            ["config-apply", playbook, context.plan.configuration.target],
            cwd=_project(context),
            timeout_seconds=timeout_seconds,
        )
        if result.timed_out:
            raise AdapterError(f"config-apply {playbook} timed out")
        return StepResult(
            status="succeeded" if result.return_code == 0 else "failed",
            message="" if result.return_code == 0 else _diagnostic(result),
        )


def _project(context: HostContext) -> Path:
    return Path(context.plan.sources.provisioning_project.path)


def _diagnostic(result: ChildResult) -> str:
    return result.stderr.strip() or f"exit status {result.return_code}"
