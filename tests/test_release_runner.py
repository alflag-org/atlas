from __future__ import annotations

import importlib
import os
import runpy
import shutil
import sys
from importlib import util as importlib_util
from importlib.machinery import ModuleSpec
from pathlib import Path
from re import escape
from types import ModuleType

import pytest

from atlas import release_runner
from atlas.releases import release_digest


@pytest.fixture
def selected_release(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "release"
    modules = root / "modules"
    modules.mkdir(parents=True)
    (modules / "runner_target.py").write_text(
        "def returns_none(argv):\n"
        "    assert argv == ['one']\n"
        "\n"
        "def returns_int(argv):\n"
        "    return len(argv) + 6\n"
        "\n"
        "def returns_bool(argv):\n"
        "    return True\n"
        "\n"
        "def returns_text(argv):\n"
        "    return 'invalid'\n"
        "\n"
        "def no_args():\n"
        "    return 0\n"
        "\n"
        "def required_positional(argv, required):\n"
        "    return 0\n"
        "\n"
        "def required_keyword(argv, *, required):\n"
        "    return 0\n"
        "\n"
        "async def async_target(argv):\n"
        "    return 0\n"
        "\n"
        "class NotAFunction:\n"
        "    pass\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ATLAS_RELEASE_ROOT", str(root))
    monkeypatch.syspath_prepend(str(modules))
    importlib.invalidate_caches()
    for name in (
        "runner_target",
        "runner_outside",
        "runner_missing",
    ):
        sys.modules.pop(name, None)
    return root


def test_target_parts_and_module_selection(selected_release: Path) -> None:
    assert release_runner._target_parts("runner_target:returns_none") == (
        "runner_target",
        "returns_none",
    )
    assert release_runner._target_parts("runner_target.returns_none") is None

    module = importlib.import_module("runner_target")
    assert release_runner._module_is_selected(module) is True

    no_origin = ModuleType("no_origin")
    assert release_runner._module_is_selected(no_origin) is False

    no_file = ModuleType("no_file")
    no_file.__file__ = str(selected_release / "modules")
    assert release_runner._module_is_selected(no_file) is False


def test_module_selection_requires_release_root(monkeypatch: pytest.MonkeyPatch) -> None:
    module = ModuleType("module")
    module.__file__ = __file__
    monkeypatch.delenv("ATLAS_RELEASE_ROOT", raising=False)
    assert release_runner._module_is_selected(module) is False


def test_runner_invokes_targets_and_validates_results(selected_release: Path) -> None:
    assert release_runner.run_target("runner_target:returns_none", ["one"]) == 0
    assert release_runner.run_target("runner_target:returns_int", ["one"]) == 7

    with pytest.raises(ValueError, match=escape("target must be package.module:callable")):
        release_runner.run_target("runner_target.returns_int", [])
    with pytest.raises(TypeError, match="target must return int or None"):
        release_runner.run_target("runner_target:returns_bool", [])
    with pytest.raises(TypeError, match="target must return int or None"):
        release_runner.run_target("runner_target:returns_text", [])


def test_runner_rechecks_selected_snapshot_digest(
    monkeypatch: pytest.MonkeyPatch,
    selected_release: Path,
    tmp_path: Path,
) -> None:
    digest = release_digest(selected_release)
    snapshot = tmp_path / f"release-{digest}"
    shutil.copytree(selected_release, snapshot)
    monkeypatch.setenv("ATLAS_RELEASE_ROOT", str(snapshot))
    monkeypatch.setenv("ATLAS_RELEASE_DIGEST", digest)

    assert release_runner.run_target("runner_target:returns_int", []) == 6
    (snapshot / "modules/runner_target.py").write_text(
        (snapshot / "modules/runner_target.py").read_text(encoding="utf-8")
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="content digest changed"):
        release_runner.run_target("runner_target:returns_int", [])

    monkeypatch.setenv("ATLAS_RELEASE_DIGEST", "bad")
    with pytest.raises(ValueError, match="digest is invalid"):
        release_runner.run_target("runner_target:returns_int", [])


def test_snapshot_digest_rejects_symlinks_and_non_regular_entries(
    tmp_path: Path,
) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    (root / "module.py").write_text("x = 1\n", encoding="utf-8")
    assert len(release_runner._snapshot_digest(root)) == 64

    root_link = tmp_path / "snapshot-link"
    root_link.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError, match="selected release root"):
        release_runner._snapshot_digest(root_link)

    symlink = root / "link"
    symlink.symlink_to(root / "module.py")
    with pytest.raises(ValueError, match="contains a symlink"):
        release_runner._snapshot_digest(root)
    symlink.unlink()

    fifo = root / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="not a regular file"):
        release_runner._snapshot_digest(root)


def test_runner_rejects_digest_named_for_another_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    selected_release: Path,
) -> None:
    digest = release_digest(selected_release)
    monkeypatch.setenv("ATLAS_RELEASE_DIGEST", digest)
    with pytest.raises(ValueError, match="snapshot name"):
        release_runner.run_target("runner_target:returns_int", [])


@pytest.mark.parametrize(
    ("callable_name", "message"),
    [
        ("missing", "target callable is not a function"),
        ("NotAFunction", "target callable is not a function"),
        ("no_args", "target callable must accept argv"),
        ("required_positional", "required positional arguments beyond argv"),
        ("required_keyword", "required keyword-only arguments"),
        ("async_target", "must be a synchronous function"),
    ],
)
def test_runner_rejects_invalid_callables(
    selected_release: Path,
    callable_name: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        release_runner.run_target(f"runner_target:{callable_name}", [])


def test_runner_rejects_static_contract_bypasses_before_import(
    selected_release: Path,
) -> None:
    modules = selected_release / "modules"
    (modules / "runner_decorated.py").write_text(
        "def decorate(function):\n"
        "    return function\n\n"
        "@decorate\n"
        "def main(argv):\n"
        "    return 0\n",
        encoding="utf-8",
    )
    (modules / "runner_wildcard.py").write_text(
        "from runner_dependency import *\n\n"
        "def main(argv):\n"
        "    return 0\n",
        encoding="utf-8",
    )
    (modules / "runner_dependency.py").write_text(
        "value = 1\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="decorators are not allowed"):
        release_runner.run_target("runner_decorated:main", [])
    with pytest.raises(ValueError, match="wildcard import"):
        release_runner.run_target("runner_wildcard:main", [])


def test_runner_rejects_import_failures_and_unselected_modules(
    monkeypatch: pytest.MonkeyPatch,
    selected_release: Path,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="could not be imported"):
        release_runner.run_target("runner_missing:main", [])

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "runner_outside.py").write_text(
        "def main(argv):\n    return 0\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(outside))
    importlib.invalidate_caches()
    with pytest.raises(ValueError, match="outside the selected release"):
        release_runner.run_target("runner_outside:main", [])


def test_runner_import_and_path_validation_edges(
    monkeypatch: pytest.MonkeyPatch,
    selected_release: Path,
    tmp_path: Path,
) -> None:
    source = selected_release / "modules/runner_target.py"
    original_spec = release_runner.importlib.util.spec_from_file_location
    monkeypatch.setattr(
        release_runner.importlib.util,
        "spec_from_file_location",
        lambda *args, **kwargs: None,
    )
    with pytest.raises(ValueError, match="could not be imported"):
        release_runner._load_module(source, "runner_target", is_package=False)
    monkeypatch.setattr(
        release_runner.importlib.util,
        "spec_from_file_location",
        original_spec,
    )

    class FailingLoader:
        def create_module(self, spec):
            return None

        def exec_module(self, module):
            raise RuntimeError("import failed")

    monkeypatch.setattr(
        release_runner.importlib.util,
        "spec_from_file_location",
        lambda *args, **kwargs: ModuleSpec("runner_target", FailingLoader()),
    )
    with pytest.raises(ValueError, match="could not be imported"):
        release_runner._load_module(source, "runner_target", is_package=False)
    monkeypatch.setattr(
        release_runner.importlib.util,
        "spec_from_file_location",
        original_spec,
    )

    monkeypatch.delenv("ATLAS_RELEASE_ROOT", raising=False)
    with pytest.raises(ValueError, match="selected release root is required"):
        release_runner._selected_target_sources("runner_target")

    selected_modules = selected_release / "modules"
    monkeypatch.setattr(sys, "path", ["", str(selected_modules)])
    assert release_runner._external_target_source_exists(
        "runner_target", selected_modules
    ) is False

    broken_root = tmp_path / "broken"
    original_resolve = Path.resolve

    def fail_external_resolve(path: Path, *args, **kwargs):
        if path == broken_root:
            raise OSError("path disappeared")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fail_external_resolve)
    monkeypatch.setattr(sys, "path", [str(broken_root)])
    assert release_runner._external_target_source_exists(
        "runner_target", selected_modules
    ) is False

    monkeypatch.setenv("ATLAS_RELEASE_ROOT", str(selected_release))
    monkeypatch.setattr(release_runner, "validate_selected_module", lambda *args: False)
    with pytest.raises(ValueError, match="outside the selected release"):
        release_runner._load_callable("runner_target", "returns_int")


def test_standalone_runner_fails_closed_when_contract_helper_cannot_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_spec = importlib_util.spec_from_file_location
    monkeypatch.setattr(importlib_util, "spec_from_file_location", lambda *args, **kwargs: None)
    with pytest.raises(RuntimeError, match="contract helper is unavailable"):
        runpy.run_path(str(Path(release_runner.__file__)), run_name="__main__")
    monkeypatch.setattr(importlib_util, "spec_from_file_location", original_spec)


def test_runner_loads_every_dotted_parent_from_selected_release(
    monkeypatch: pytest.MonkeyPatch,
    selected_release: Path,
    tmp_path: Path,
) -> None:
    selected_marker = tmp_path / "selected-parent-marker"
    external_marker = tmp_path / "external-parent-marker"
    package = selected_release / "modules/pkg/sub"
    package.mkdir(parents=True)
    (selected_release / "modules/pkg/__init__.py").write_text(
        f"from pathlib import Path\nPath({str(selected_marker)!r}).write_text('selected')\n",
        encoding="utf-8",
    )
    (package / "__init__.py").write_text("VALUE = 'selected'\n", encoding="utf-8")
    (package / "target.py").write_text(
        "def main(argv):\n    return 0\n",
        encoding="utf-8",
    )
    external = tmp_path / "external"
    (external / "pkg/sub").mkdir(parents=True)
    (external / "pkg/__init__.py").write_text(
        f"from pathlib import Path\nPath({str(external_marker)!r}).write_text('external')\n",
        encoding="utf-8",
    )
    (external / "pkg/sub/__init__.py").write_text("VALUE = 'external'\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(external))

    assert release_runner.run_target("pkg.sub.target:main", []) == 0
    assert selected_marker.read_text(encoding="utf-8") == "selected"
    assert not external_marker.exists()


def test_invalid_dotted_target_never_executes_external_parent_initializer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    release = tmp_path / "release"
    modules = release / "modules"
    modules.mkdir(parents=True)
    (release / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    (release / "release.yml").write_text(
        "schema: atlas.release/v1\n"
        "name: sample\n"
        "commands:\n"
        "  sample:\n"
        "    target: foreign.child:main\n",
        encoding="utf-8",
    )
    marker = tmp_path / "external-initializer-ran"
    external = tmp_path / "external"
    (external / "foreign").mkdir(parents=True)
    (external / "foreign/__init__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
        encoding="utf-8",
    )
    (external / "foreign/child.py").write_text(
        "def main(argv):\n    return 0\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(external))
    monkeypatch.setenv("ATLAS_RELEASE_ROOT", str(release))

    from atlas.manifests import load_manifest

    with pytest.raises(ValueError, match="parent package"):
        load_manifest(release)
    with pytest.raises(ValueError, match="outside the selected release"):
        release_runner.run_target("foreign.child:main", [])
    assert not marker.exists()


def test_runner_main_uses_supplied_and_process_arguments(
    monkeypatch: pytest.MonkeyPatch,
    selected_release: Path,
) -> None:
    assert release_runner.main(["runner_target:returns_int", "one"]) == 7
    monkeypatch.setattr(
        sys,
        "argv",
        ["atlas_release_runner.py", "runner_target:returns_none", "one"],
    )
    assert release_runner.main() == 0
    with pytest.raises(ValueError, match="target is required"):
        release_runner.main([])


def test_runner_module_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
    selected_release: Path,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["atlas_release_runner.py", "runner_target:returns_int", "one"],
    )
    with pytest.raises(SystemExit) as raised:
        runpy.run_path(str(Path(release_runner.__file__)), run_name="__main__")
    assert raised.value.code == 7
