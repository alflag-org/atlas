from __future__ import annotations

import runpy
import sys
from pathlib import Path

import atlas_infrastructure_operations.child as child_module
import atlas_infrastructure_operations.imagectl as imagectl_module
import pytest

ROOT = Path(__file__).resolve().parents[1]
INFRASTRUCTURE_OPERATIONS = ROOT / "infrastructure-operations"


def test_imagectl_dispatches_private_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        imagectl_module,
        "run_job",
        lambda job, args: calls.append((job, args)) or 7,
    )

    assert imagectl_module.main(["plan", "provider.yml", "image.yml"]) == 7
    assert calls[-1] == (
        "vm-template-create-plan",
        ["provider.yml", "image.yml"],
    )
    assert (
        imagectl_module.main(
            ["apply", "provider.yml", "plan.json", "--confirm", "plan-1"]
        )
        == 7
    )
    assert calls[-1] == (
        "vm-template-create-apply",
        ["provider.yml", "plan.json", "--confirm", "plan-1"],
    )
    assert imagectl_module.main(["verify", "provider.yml"]) == 7
    assert calls[-1] == ("vm-template-create-verify", ["provider.yml", "-"])
    assert (
        imagectl_module.main(
            ["rollback", "provider.yml", "--confirm", "plan-1"]
        )
        == 7
    )
    assert calls[-1] == (
        "vm-template-create-rollback",
        ["provider.yml", "-", "--confirm", "plan-1"],
    )

    with pytest.raises(SystemExit) as raised:
        imagectl_module.main(["apply", "provider.yml"])
    assert raised.value.code == 2
    for removed in (["status", "image-1"], ["resume", "image-1"]):
        with pytest.raises(SystemExit) as raised:
            imagectl_module.main(removed)
        assert raised.value.code == 2


def test_infrastructure_child_contract(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original_run_child = child_module.run_child
    monkeypatch.setenv("ATLAS_EXECUTABLE", "/custom/atlas")
    assert child_module.atlas_executable() == "/custom/atlas"
    assert child_module.job_argv("vm-template-create-plan", ["provider.yml"]) == [
        "/custom/atlas",
        "job",
        "run",
        "infrastructure-operations",
        "vm-template-create-plan",
        "--",
        "provider.yml",
    ]
    monkeypatch.delenv("ATLAS_EXECUTABLE")
    monkeypatch.setenv("ATLAS_HOME", "/srv/atlas")
    assert child_module.atlas_executable() == "/srv/atlas/bin/atlas"
    monkeypatch.delenv("ATLAS_HOME")
    assert child_module.atlas_executable() == "/opt/atlas/bin/atlas"

    calls: list[tuple[list[str], dict[str, object]]] = []

    class Process:
        returncode = -2

    monkeypatch.setattr(
        child_module.subprocess,
        "run",
        lambda argv, **kwargs: calls.append((argv, kwargs)) or Process(),
    )
    assert child_module.run_child(["child"]) == 130
    assert calls == [(["child"], {"check": False, "shell": False})]

    monkeypatch.setattr(child_module, "run_child", lambda argv: len(argv))
    assert child_module.run_job("vm-template-create-plan", ["provider.yml"]) == 7

    monkeypatch.setattr(
        child_module.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    )
    assert original_run_child(["missing"]) == 127
    assert "missing command not found" in capsys.readouterr().err


def test_infrastructure_controller_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entrypoint = INFRASTRUCTURE_OPERATIONS / "commands/imagectl.py"
    namespace = runpy.run_path(
        str(entrypoint),
        run_name="infrastructure_entrypoint_test",
    )
    assert namespace["__name__"] == "infrastructure_entrypoint_test"
    monkeypatch.setattr(imagectl_module, "main", lambda: 7)
    with pytest.raises(SystemExit) as raised:
        runpy.run_path(str(entrypoint), run_name="__main__")
    assert raised.value.code == 7


@pytest.mark.parametrize(
    "job_name",
    [
        "vm-create-plan",
        "vm-create-apply",
        "vm-create-verify",
        "vm-create-rollback",
        "vm-template-create-plan",
        "vm-template-create-apply",
        "vm-template-create-verify",
        "vm-template-create-rollback",
    ],
)
def test_infrastructure_diagnostic_job_entrypoints_show_help(
    job_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", [job_name, "--help"])
    with pytest.raises(SystemExit) as raised:
        runpy.run_path(
            str(INFRASTRUCTURE_OPERATIONS / "jobs" / f"{job_name}.py"),
            run_name="__main__",
        )
    assert raised.value.code == 0


def test_public_controllers_do_not_import_operation_implementations() -> None:
    for path in (
        INFRASTRUCTURE_OPERATIONS
        / "modules/atlas_infrastructure_operations/imagectl.py",
        ROOT
        / "configuration-operations/modules/atlas_configuration_operations/controller.py",
    ):
        source = path.read_text(encoding="utf-8")
        assert "atlas_operations.operation" not in source
        assert "shell=True" not in source
