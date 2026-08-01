from __future__ import annotations

import runpy
from pathlib import Path

import atlas_host_operations.controller as controller_module
import pytest
from atlas_host_operations.lifecycle import ProvisioningPhase

INFRASTRUCTURE_OPERATIONS = Path(__file__).parents[1] / "infrastructure-operations"


def test_entrypoints_do_not_execute_when_loaded() -> None:
    entrypoints = [INFRASTRUCTURE_OPERATIONS / "commands" / "hostctl.py"]
    entrypoints.extend(sorted((INFRASTRUCTURE_OPERATIONS / "jobs").glob("*.py")))

    for entrypoint in entrypoints:
        namespace = runpy.run_path(str(entrypoint), run_name="host_entrypoint_test")
        assert namespace["__name__"] == "host_entrypoint_test"


def test_hostctl_command_entrypoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(controller_module, "main", lambda: 7)
    with pytest.raises(SystemExit) as raised:
        runpy.run_path(
            str(INFRASTRUCTURE_OPERATIONS / "commands" / "hostctl.py"),
            run_name="__main__",
        )
    assert raised.value.code == 7


@pytest.mark.parametrize(
    ("name", "phase"),
    [
        ("host-registry-reserve", ProvisioningPhase.RESERVE),
        ("host-provider-allocate", ProvisioningPhase.ALLOCATE),
        ("host-provider-verify", ProvisioningPhase.PROVIDER_VERIFY),
        ("host-registry-bind", ProvisioningPhase.BIND),
        ("host-wait-ready", ProvisioningPhase.WAIT_READY),
        ("host-config-bootstrap", ProvisioningPhase.BOOTSTRAP),
        ("host-config-converge", ProvisioningPhase.CONVERGE),
        ("host-config-verify", ProvisioningPhase.CONFIGURATION_VERIFY),
        ("host-registry-activate", ProvisioningPhase.ACTIVATE),
    ],
)
def test_phase_job_entrypoints(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    phase: ProvisioningPhase,
) -> None:
    calls: list[ProvisioningPhase] = []
    monkeypatch.setattr(
        controller_module,
        "phase_job_main",
        lambda value: calls.append(value) or 0,
    )
    with pytest.raises(SystemExit) as raised:
        runpy.run_path(
            str(INFRASTRUCTURE_OPERATIONS / "jobs" / f"{name}.py"),
            run_name="__main__",
        )
    assert raised.value.code == 0
    assert calls == [phase]


def test_reconcile_job_entrypoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(controller_module, "reconcile_job_main", lambda: 6)
    with pytest.raises(SystemExit) as raised:
        runpy.run_path(
            str(
                INFRASTRUCTURE_OPERATIONS
                / "jobs"
                / "host-operation-reconcile.py"
            ),
            run_name="__main__",
        )
    assert raised.value.code == 6
