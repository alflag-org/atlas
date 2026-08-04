from __future__ import annotations

from pathlib import Path

import atlas_host_operations.controller as controller_module
import pytest
from atlas_host_operations.lifecycle import ProvisioningPhase

from atlas.manifests import load_manifest

ROOT = Path(__file__).parents[1]
OPERATIONS = ROOT / "operations"


def test_release_has_no_wrapper_directories() -> None:
    manifest = load_manifest(OPERATIONS)
    assert not (OPERATIONS / "commands").exists()
    assert not (OPERATIONS / "jobs").exists()
    assert manifest.commands["hostctl"].target.spec == (
        "atlas_host_operations.controller:main"
    )


@pytest.mark.parametrize(
    ("name", "function", "phase"),
    [
        ("host-registry-reserve", "host_registry_reserve_main", ProvisioningPhase.RESERVE),
        ("host-provider-allocate", "host_provider_allocate_main", ProvisioningPhase.ALLOCATE),
        ("host-provider-verify", "host_provider_verify_main", ProvisioningPhase.PROVIDER_VERIFY),
        ("host-registry-bind", "host_registry_bind_main", ProvisioningPhase.BIND),
        ("host-wait-ready", "host_wait_ready_main", ProvisioningPhase.WAIT_READY),
        ("host-config-bootstrap", "host_config_bootstrap_main", ProvisioningPhase.BOOTSTRAP),
        ("host-config-converge", "host_config_converge_main", ProvisioningPhase.CONVERGE),
        (
            "host-config-verify",
            "host_config_verify_main",
            ProvisioningPhase.CONFIGURATION_VERIFY,
        ),
        ("host-registry-activate", "host_registry_activate_main", ProvisioningPhase.ACTIVATE),
    ],
)
def test_phase_manifest_targets(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    function: str,
    phase: ProvisioningPhase,
) -> None:
    calls: list[tuple[ProvisioningPhase, list[str] | None]] = []
    monkeypatch.setattr(
        controller_module,
        "phase_job_main",
        lambda value, argv=None: calls.append((value, argv)) or 0,
    )
    assert getattr(controller_module, function)(["--plan", "-"]) == 0
    assert calls == [(phase, ["--plan", "-"])]
    assert load_manifest(OPERATIONS).jobs[name].target.callable_name == function


def test_reconcile_manifest_target(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(controller_module, "reconcile_job_main", lambda argv=None: 6)
    assert controller_module.reconcile_job_main(["--plan", "-"]) == 6
    assert (
        load_manifest(OPERATIONS).jobs["host-operation-reconcile"].target.spec
        == "atlas_host_operations.controller:reconcile_job_main"
    )
