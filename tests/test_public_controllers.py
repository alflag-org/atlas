from __future__ import annotations

from pathlib import Path

import atlas_image_operations.imagectl as imagectl_module
import atlas_operations.child as child_module
import pytest

from atlas.manifests import load_manifest

ROOT = Path(__file__).resolve().parents[1]
OPERATIONS = ROOT / "operations"


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


def test_operations_child_contract(
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
        "operations",
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


def test_image_controller_is_manifest_target() -> None:
    assert load_manifest(OPERATIONS).commands["imagectl"].target.spec == (
        "atlas_image_operations.imagectl:main"
    )


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
def test_operation_diagnostic_job_targets_show_help(
    job_name: str,
) -> None:
    job = load_manifest(OPERATIONS).jobs[job_name]
    module_name = job.target.module
    callable_name = job.target.callable_name
    module = __import__(module_name, fromlist=[callable_name])
    target = getattr(module, callable_name)
    with pytest.raises(SystemExit) as raised:
        target(["--help"])
    assert raised.value.code == 0


def test_public_controllers_do_not_import_operation_implementations() -> None:
    for path in (
        OPERATIONS / "modules/atlas_image_operations/imagectl.py",
        ROOT
        / "operations/modules/atlas_configuration_operations/controller.py",
    ):
        source = path.read_text(encoding="utf-8")
        assert "atlas_operations.operation" not in source
        assert "shell=True" not in source
