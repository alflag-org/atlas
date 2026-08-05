from __future__ import annotations

import ast
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from atlas.catalog import active_releases, command_index, resolve_service
from atlas.manifests import (
    ExecutableArtifact,
    ReleaseManifest,
    Target,
    load_manifest,
    validate_name,
)
from atlas.releases import read_version, validate_release
from atlas.target_contract import (
    TargetResolutionError,
    TargetSources,
    _annotation_is_int_or_none,
    _bound_names,
    _module_bindings,
    _selected_path,
    parse_target_spec,
    resolve_target_sources,
    validate_callable_runtime,
    validate_selected_module,
)


def _release(
    path: Path,
    *,
    manifest: str | None = None,
    command_files: tuple[str, ...] = ("sample.py",),
    version: str = "1.0.0",
) -> Path:
    path.mkdir(parents=True)
    (path / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    modules = path / "modules"
    modules.mkdir()
    for command_file in command_files:
        module_name = Path(command_file).stem.replace("-", "_")
        module_file = modules / f"{module_name}.py"
        module_file.write_text(
            "def main(argv: list[str] | None = None) -> int:\n"
            "    return 0\n",
            encoding="utf-8",
        )
    if manifest is None:
        manifest = (
            "schema: atlas.release/v1\n"
            "name: sample\n"
            "commands:\n"
            "  sample:\n"
            "    target: sample:main\n"
        )
    (path / "release.yml").write_text(manifest, encoding="utf-8")
    return path


def test_load_manifest_declares_commands_explicitly(tmp_path: Path) -> None:
    release = _release(
        tmp_path / "release",
        manifest=(
            "schema: atlas.release/v1\n"
            "name: operations\n"
            "commands:\n"
            "  config-check:\n"
            "    target: config_check:main\n"
            "  inventory-show:\n"
            "    target: inventory_show:main\n"
        ),
        command_files=(
            "config-check.py",
            "inventory-show.py",
            "not-declared.py",
        ),
    )

    manifest = load_manifest(release)

    assert manifest == ReleaseManifest(
        name="operations",
        commands={
            "config-check": ExecutableArtifact(
                name="config-check",
                target=Target("config_check", "main"),
            ),
            "inventory-show": ExecutableArtifact(
                name="inventory-show",
                target=Target("inventory_show", "main"),
            ),
        },
        jobs={},
        services={},
    )
    assert "not-declared" not in manifest.commands


def test_load_manifest_allows_a_release_without_commands(tmp_path: Path) -> None:
    release = _release(
        tmp_path / "release",
        manifest="schema: atlas.release/v1\nname: assets-only\n",
    )

    assert load_manifest(release).commands == {}


def test_load_manifest_declares_non_public_jobs(tmp_path: Path) -> None:
    release = _release(
        tmp_path / "release",
        manifest=(
            "schema: atlas.release/v1\n"
            "name: worker\n"
            "commands: {}\n"
            "jobs:\n"
            "  inventory-refresh:\n"
            "    target: inventory_refresh:main\n"
            "    default_timeout_seconds: 300\n"
        ),
        command_files=("inventory-refresh.py",),
    )

    manifest = load_manifest(release)

    assert manifest.commands == {}
    assert manifest.jobs["inventory-refresh"] == ExecutableArtifact(
        name="inventory-refresh",
        target=Target("inventory_refresh", "main"),
        default_timeout_seconds=300,
    )


def _read_manifest(root: Path) -> dict:
    raw = yaml.safe_load((root / "release.yml").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def _write_manifest(root: Path, raw: dict) -> None:
    (root / "release.yml").write_text(
        yaml.safe_dump(raw, sort_keys=False),
        encoding="utf-8",
    )


def test_load_manifest_declares_job_service_and_catalog_resolves_it(
    release_factory,
    tmp_path: Path,
) -> None:
    root = release_factory(
        name="operations",
        commands=("config-show",),
        jobs=("state-collect",),
        timeout=30,
        service="state-collect",
    )

    manifest = load_manifest(root)

    service = manifest.services["state-collect"]
    assert service.name == "state-collect"
    assert service.job == "state-collect"
    assert service.command is None
    assert service.systemd.service == (
        root / "init/systemd/state-collect.service"
    ).resolve()
    assert service.systemd.timer == (
        root / "init/systemd/state-collect.timer"
    ).resolve()

    current = tmp_path / "current"
    current.mkdir()
    (current / "operations").symlink_to(root, target_is_directory=True)
    assert resolve_service(
        current,
        "operations",
        "state-collect",
    ).service == service
    with pytest.raises(ValueError, match="unknown service"):
        resolve_service(current, "operations", "missing")
    with pytest.raises(ValueError, match="unknown release"):
        resolve_service(current, "missing", "state-collect")


@pytest.mark.parametrize(
    ("mutate", "expected_exception", "message"),
    [
        (
            lambda raw: raw.update(services=[]),
            TypeError,
            "services must be a mapping",
        ),
        (
            lambda raw: raw["services"].update({1: {}}),
            TypeError,
            "service name must be a string",
        ),
        (
            lambda raw: raw["services"].update({"Bad": {}}),
            ValueError,
            "invalid service name",
        ),
        (
            lambda raw: raw["services"].update({"refresh": []}),
            TypeError,
            "services.refresh must be a mapping",
        ),
        (
            lambda raw: raw["services"]["refresh"].update(extra=True),
            ValueError,
            "has unknown key",
        ),
        (
            lambda raw: raw["services"]["refresh"].update(command="sample-show"),
            ValueError,
            "exactly one command or job",
        ),
        (
            lambda raw: raw["services"]["refresh"].pop("job"),
            ValueError,
            "exactly one command or job",
        ),
        (
            lambda raw: (
                raw["services"]["refresh"].pop("job"),
                raw["services"]["refresh"].update(command=1),
            ),
            ValueError,
            "references an unknown command",
        ),
        (
            lambda raw: (
                raw["services"]["refresh"].pop("job"),
                raw["services"]["refresh"].update(command="missing"),
            ),
            ValueError,
            "references an unknown command",
        ),
        (
            lambda raw: raw["services"]["refresh"].update(job=1),
            ValueError,
            "references an unknown job",
        ),
        (
            lambda raw: raw["services"]["refresh"].update(job="missing"),
            ValueError,
            "references an unknown job",
        ),
        (
            lambda raw: raw["services"]["refresh"].update(init=[]),
            TypeError,
            "init must be a mapping",
        ),
        (
            lambda raw: raw["services"]["refresh"]["init"].update(openrc={}),
            ValueError,
            "init has unknown key",
        ),
        (
            lambda raw: raw["services"]["refresh"]["init"].update(systemd=[]),
            TypeError,
            "systemd must be a mapping",
        ),
        (
            lambda raw: raw["services"]["refresh"]["init"]["systemd"].update(
                extra=True
            ),
            ValueError,
            "systemd has unknown key",
        ),
        (
            lambda raw: raw["services"]["refresh"]["init"]["systemd"].pop(
                "service"
            ),
            ValueError,
            "service is required",
        ),
        (
            lambda raw: raw["services"]["refresh"]["init"]["systemd"].update(
                timer=1
            ),
            ValueError,
            "timer must be a non-empty string",
        ),
        (
            lambda raw: raw["services"]["refresh"]["init"]["systemd"].update(
                timer=" "
            ),
            ValueError,
            "timer must be a non-empty string",
        ),
        (
            lambda raw: raw["services"]["refresh"]["init"]["systemd"].update(
                service="../escape.service"
            ),
            ValueError,
            "must be a relative path inside the release",
        ),
        (
            lambda raw: raw["services"]["refresh"]["init"]["systemd"].update(
                service="init/systemd/missing.service"
            ),
            ValueError,
            "not found",
        ),
    ],
)
def test_load_manifest_rejects_invalid_services(
    release_factory,
    mutate,
    expected_exception: type[Exception],
    message: str,
) -> None:
    root = release_factory(
        jobs=("collect",),
        service="refresh",
    )
    raw = _read_manifest(root)
    mutate(raw)
    _write_manifest(root, raw)

    with pytest.raises(expected_exception, match=message):
        load_manifest(root)


def test_manifest_supports_command_service_without_timer(
    release_factory,
) -> None:
    root = release_factory(
        commands=("sample-show",),
        jobs=("collect",),
        service="refresh",
    )
    raw = _read_manifest(root)
    service = raw["services"]["refresh"]
    service.pop("job")
    service["command"] = "sample-show"
    service["init"]["systemd"].pop("timer")
    _write_manifest(root, raw)

    parsed = load_manifest(root).services["refresh"]

    assert parsed.command == "sample-show"
    assert parsed.job is None
    assert parsed.systemd.timer is None


def test_manifest_service_paths_reject_symlink_escape_and_wrong_suffix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    release = _release(tmp_path / "release")
    systemd = release / "init/systemd"
    systemd.mkdir(parents=True)
    service_path = systemd / "refresh.service"
    service_path.write_text("[Service]\n", encoding="utf-8")
    raw = _read_manifest(release)
    raw["services"] = {
        "refresh": {
            "command": "sample",
            "init": {
                "systemd": {
                    "service": "init/systemd/refresh.service",
                }
            },
        }
    }
    _write_manifest(release, raw)
    assert load_manifest(release).services["refresh"].systemd.timer is None

    service_path.unlink()
    outside = tmp_path / "outside.service"
    outside.write_text("[Service]\n", encoding="utf-8")
    service_path.symlink_to(outside)
    with pytest.raises(ValueError, match="must not contain a symlink"):
        load_manifest(release)

    service_path.unlink()
    wrong_suffix = systemd / "refresh.txt"
    wrong_suffix.write_text("[Service]\n", encoding="utf-8")
    raw["services"]["refresh"]["init"]["systemd"]["service"] = (
        "init/systemd/refresh.txt"
    )
    _write_manifest(release, raw)
    with pytest.raises(ValueError, match=r"must end with \.service"):
        load_manifest(release)

    raw["services"]["refresh"]["init"]["systemd"]["service"] = (
        "init/systemd/refresh.service"
    )
    service_path = systemd / "refresh.service"
    service_path.write_text("[Service]\n", encoding="utf-8")
    _write_manifest(release, raw)
    original_resolve = Path.resolve

    def fake_resolve(path: Path, *args, **kwargs):
        if path == service_path:
            return tmp_path / "outside" / "refresh.service"
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fake_resolve)
    with pytest.raises(ValueError, match="escapes the release root"):
        load_manifest(release)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            "missing-modules",
            "module root not found",
        ),
        (
            "ambiguous",
            "module is ambiguous",
        ),
        (
            "syntax",
            "module cannot be parsed",
        ),
        (
            "async",
            "must be a synchronous function",
        ),
        (
            "no-argv",
            "must accept argv",
        ),
        (
            "required-positional",
            "required positional",
        ),
        (
            "required-keyword",
            "required keyword-only",
        ),
        (
            "duplicate",
            "target callable is not a function",
        ),
        (
            "rebind",
            "target callable is not a function",
        ),
        (
            "conditional-rebind",
            "target callable is not a function",
        ),
        (
            "async-after-sync",
            "must be a synchronous function",
        ),
        (
            "bad-return",
            "return annotation",
        ),
    ],
)
def test_manifest_target_source_and_callable_validation(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    release = _release(tmp_path / mutation)
    modules = release / "modules"
    if mutation == "missing-modules":
        modules.rename(release / "saved-modules")
    elif mutation == "ambiguous":
        (modules / "ambiguous").mkdir()
        (modules / "ambiguous.py").write_text(
            "def main(argv):\n    return 0\n", encoding="utf-8"
        )
        (modules / "ambiguous/__init__.py").write_text(
            "def main(argv):\n    return 0\n", encoding="utf-8"
        )
        raw = _read_manifest(release)
        raw["commands"]["sample"]["target"] = "ambiguous:main"
        _write_manifest(release, raw)
    else:
        contents = {
            "syntax": "def main(\n",
            "async": "async def main(argv):\n    return 0\n",
            "no-argv": "def main():\n    return 0\n",
            "required-positional": "def main(argv, required):\n    return 0\n",
            "required-keyword": "def main(argv, *, required):\n    return 0\n",
            "duplicate": (
                "def main(argv):\n    return 0\n\n"
                "def main(argv):\n    return 1\n"
            ),
            "rebind": "def main(argv):\n    return 0\nmain = 1\n",
            "conditional-rebind": (
                "def main(argv):\n    return 0\n\n"
                "if True:\n    main = 1\n"
            ),
            "async-after-sync": (
                "def main(argv):\n    return 0\n\n"
                "async def main(argv):\n    return 1\n"
            ),
            "bad-return": "def main(argv) -> str:\n    return 'bad'\n",
        }
        (modules / "sample.py").write_text(contents[mutation], encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_manifest(release)


def test_target_contract_rejects_external_parents_and_accepts_selected_package(
    tmp_path: Path,
) -> None:
    release = _release(tmp_path / "release")
    modules = release / "modules"
    package = modules / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (package / "child.py").write_text(
        "def main(argv):\n    return 0\n",
        encoding="utf-8",
    )
    sources = resolve_target_sources(release, "pkg.child")
    assert sources.package_sources == (("pkg", (package / "__init__.py").resolve()),)
    assert sources.source == (package / "child.py").resolve()
    assert sources.is_package is False

    missing_init = tmp_path / "missing-init"
    (missing_init / "modules/pkg").mkdir(parents=True)
    (missing_init / "modules/pkg/child.py").write_text(
        "def main(argv):\n    return 0\n",
        encoding="utf-8",
    )
    with pytest.raises(TargetResolutionError, match="initializer not found"):
        resolve_target_sources(missing_init, "pkg.child")

    ambiguous_parent = tmp_path / "ambiguous-parent"
    (ambiguous_parent / "modules/pkg").mkdir(parents=True)
    (ambiguous_parent / "modules/pkg.py").write_text("VALUE = 1\n", encoding="utf-8")
    (ambiguous_parent / "modules/pkg/__init__.py").write_text(
        "VALUE = 1\n", encoding="utf-8"
    )
    (ambiguous_parent / "modules/pkg/child.py").write_text(
        "def main(argv):\n    return 0\n", encoding="utf-8"
    )
    with pytest.raises(TargetResolutionError, match="parent package is ambiguous"):
        resolve_target_sources(ambiguous_parent, "pkg.child")


def test_target_contract_edge_helpers(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"package\.module"):
        parse_target_spec(" pkg:main")
    with pytest.raises(ValueError, match=r"package\.module"):
        parse_target_spec("pkg:main ")
    with pytest.raises(ValueError, match=r"package\.module"):
        parse_target_spec("pkg.main")

    modules = tmp_path / "modules"
    modules.mkdir()
    with pytest.raises(TargetResolutionError, match="escapes the release root"):
        _selected_path(tmp_path / "outside.py", modules, "outside")
    with pytest.raises(TargetResolutionError, match="not a directory"):
        resolve_target_sources(tmp_path / "missing", "sample")

    assert _bound_names(ast.parse("x = 1").body[0].targets[0]) == ["x"]
    assert _bound_names(
        ast.parse("a, *b = value").body[0].targets[0]
    ) == ["a", "b"]
    assert _bound_names(ast.parse("obj.value = 1").body[0].targets[0]) == []
    assert _annotation_is_int_or_none(ast.parse("x: int").body[0].annotation)
    assert _annotation_is_int_or_none(
        ast.parse("x: int | None").body[0].annotation
    )
    assert _annotation_is_int_or_none(
        ast.parse("x: Optional[int | None]").body[0].annotation
    )
    assert _annotation_is_int_or_none(
        ast.parse("x: typing.Optional[int]").body[0].annotation
    )
    assert _annotation_is_int_or_none(
        ast.parse("x: typing.Union[int, None]").body[0].annotation
    )
    assert not _annotation_is_int_or_none(
        ast.parse("x: list[int]").body[0].annotation
    )
    assert not _annotation_is_int_or_none(
        ast.parse("x: int | str").body[0].annotation
    )
    assert not _annotation_is_int_or_none(
        ast.parse("x: typing.Any[int]").body[0].annotation
    )
    assert not _annotation_is_int_or_none(
        ast.parse("x: other.Optional[int]").body[0].annotation
    )
    assert not _annotation_is_int_or_none(
        ast.parse("x: factory().Optional[int]").body[0].annotation
    )
    assert not _annotation_is_int_or_none(
        ast.parse('x: "int"').body[0].annotation
    )
    assert not _annotation_is_int_or_none(
        ast.parse("x: factory()").body[0].annotation
    )
    assert not _annotation_is_int_or_none(
        ast.parse("x: Optional[()]").body[0].annotation
    )
    assert _module_bindings(ast.parse("del main")).pop()[0] == "main"
    nested_bindings = _module_bindings(
        ast.parse(
            "if True:\n"
            "    import main\n"
            "    import package as package_alias\n"
            "    from package import main as imported\n"
            "    from package import main\n"
            "    from package import other\n"
            "    def main(argv):\n        return 0\n"
            "    async def main(argv):\n        return 0\n"
            "    class main:\n        pass\n"
            "    main = 1\n"
            "    main\n"
        )
    )
    assert [name for name, _, _ in nested_bindings].count("main") >= 6


def test_runtime_target_contract_and_provenance_edges(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = ModuleType("selected")
    source = tmp_path / "selected.py"
    source.write_text("", encoding="utf-8")
    module.__file__ = str(source)
    sources = TargetSources(
        modules_root=tmp_path,
        module_name="selected",
        package_sources=(),
        source=source,
        is_package=False,
    )
    assert validate_selected_module(module, sources) is True
    assert validate_selected_module(ModuleType("missing"), sources) is False

    original_resolve = Path.resolve

    def fail_resolve(path: Path, *args, **kwargs):
        if path == source:
            raise OSError("source disappeared")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fail_resolve)
    assert validate_selected_module(module, sources) is False

    with pytest.raises(ValueError, match="not a function"):
        validate_callable_runtime(1, "selected", "main")

    async def async_target(argv):
        return None

    with pytest.raises(ValueError, match="synchronous"):
        validate_callable_runtime(async_target, "selected", "main")

    def valid_target(argv):
        return None

    with pytest.raises(ValueError, match="signature is unavailable"):
        monkeypatch.setattr(
            "atlas.target_contract.inspect.signature",
            lambda target: (_ for _ in ()).throw(TypeError("no signature")),
        )
        validate_callable_runtime(valid_target, "selected", "main")


@pytest.mark.parametrize(
    ("jobs", "expected_exception", "message"),
    [
        ("[]", TypeError, "jobs must be a mapping"),
        ("{1: {}}", TypeError, "job name must be a string"),
        ("{Bad: {}}", ValueError, "invalid job name"),
        (
            "{collect: {target: collect:main, extra: true}}",
            ValueError,
            "jobs.collect has unknown key: extra",
        ),
        (
            "{collect: {target: collect:main, "
            "default_timeout_seconds: 0}}",
            ValueError,
            "must be a positive integer",
        ),
        (
            "{collect: {target: collect:main, "
            "default_timeout_seconds: true}}",
            ValueError,
            "must be a positive integer",
        ),
    ],
)
def test_load_manifest_rejects_invalid_jobs(
    tmp_path: Path,
    jobs: str,
    expected_exception: type[Exception],
    message: str,
) -> None:
    release = _release(
        tmp_path / "release",
        manifest=(
            "schema: atlas.release/v1\n"
            "name: worker\n"
            "commands: {}\n"
            f"jobs: {jobs}\n"
        ),
        command_files=("collect.py",),
    )

    with pytest.raises(expected_exception, match=message):
        load_manifest(release)


def test_load_manifest_rejects_command_job_name_overlap(tmp_path: Path) -> None:
    release = _release(
        tmp_path / "release",
        manifest=(
            "schema: atlas.release/v1\n"
            "name: worker\n"
            "commands:\n"
            "  collect:\n"
            "    target: sample:main\n"
            "jobs:\n"
            "  collect:\n"
            "    target: collect:main\n"
        ),
        command_files=("sample.py", "collect.py"),
    )

    with pytest.raises(ValueError, match="command and job names overlap: collect"):
        load_manifest(release)


@pytest.mark.parametrize(
    ("manifest", "expected_exception", "message"),
    [
        ("[]\n", TypeError, "release.yml must be a mapping"),
        (
            "schema: atlas.release/v1\nname: sample\ncommands: {}\nextra: true\n",
            ValueError,
            "release.yml has unknown key: extra",
        ),
        ("name: sample\ncommands: {}\n", ValueError, "release.yml.schema is required"),
        (
            "schema: atlas.release/v2\nname: sample\ncommands: {}\n",
            ValueError,
            "unsupported release schema",
        ),
        ("schema: atlas.release/v1\ncommands: {}\n", ValueError, "release.yml.name is required"),
        (
            "schema: atlas.release/v1\nname: bad_name\ncommands: {}\n",
            ValueError,
            "invalid release name",
        ),
        (
            "schema: atlas.release/v1\nname: sample\ncommands: []\n",
            TypeError,
            "commands must be a mapping",
        ),
        (
            "schema: atlas.release/v1\nname: sample\ncommands:\n  1: {}\n",
            TypeError,
            "command name must be a string",
        ),
        (
            "schema: atlas.release/v1\nname: sample\ncommands:\n  Bad: {}\n",
            ValueError,
            "invalid command name",
        ),
        (
            "schema: atlas.release/v1\nname: sample\ncommands:\n  atlas: {}\n",
            ValueError,
            "reserved command name",
        ),
        (
            "schema: atlas.release/v1\nname: sample\ncommands:\n  sample: []\n",
            TypeError,
            "commands.sample must be a mapping",
        ),
        (
            "schema: atlas.release/v1\nname: sample\ncommands:\n"
            "  sample:\n    target: sample:main\n    extra: true\n",
            ValueError,
            "commands.sample has unknown key: extra",
        ),
        (
            "schema: atlas.release/v1\nname: sample\ncommands:\n"
            "  sample:\n    target: \n",
            ValueError,
            "commands.sample.target is required",
        ),
        (
            "schema: atlas.release/v1\nname: sample\ncommands:\n"
            "  sample:\n    target: sample:main\n    runtime: shell\n",
            ValueError,
            "commands.sample has unknown key: runtime",
        ),
        (
            "schema: atlas.release/v1\nname: sample\ncommands:\n"
            "  sample:\n    other: true\n",
            ValueError,
            "commands.sample has unknown key: other",
        ),
    ],
)
def test_load_manifest_rejects_invalid_shapes(
    tmp_path: Path,
    manifest: str,
    expected_exception: type[Exception],
    message: str,
) -> None:
    release = _release(tmp_path / "release", manifest=manifest)

    with pytest.raises(expected_exception, match=message):
        load_manifest(release)


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ("/tmp/sample.py", "must be package.module:callable"),
        ("../sample", "must be package.module:callable"),
        (r"commands\\sample", "must be package.module:callable"),
        ("missing:main", "target module not found"),
        ("sample:missing", "target callable is not a function"),
    ],
)
def test_load_manifest_rejects_unsafe_targets(
    tmp_path: Path,
    target: str,
    message: str,
) -> None:
    release = _release(
        tmp_path / "release",
        manifest=(
            "schema: atlas.release/v1\n"
            "name: sample\n"
            "commands:\n"
            "  sample:\n"
            f"    target: {target}\n"
        ),
        command_files=("sample.py",),
    )

    with pytest.raises(ValueError, match=message):
        load_manifest(release)


def test_load_manifest_rejects_target_and_parent_symlinks(tmp_path: Path) -> None:
    outside = tmp_path / "outside.py"
    outside.write_text("def main(argv):\n    return 0\n", encoding="utf-8")
    release = _release(tmp_path / "release")
    (release / "modules/sample.py").unlink()
    (release / "modules/sample.py").symlink_to(outside)
    with pytest.raises(ValueError, match="must not contain a symlink"):
        load_manifest(release)

    (release / "modules/sample.py").unlink()
    (release / "modules").rename(release / "real-modules")
    (release / "modules").symlink_to(release / "real-modules", target_is_directory=True)
    with pytest.raises(ValueError, match="must not contain a symlink"):
        load_manifest(release)


def test_load_manifest_rejects_resolved_escape(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    release = _release(tmp_path / "release")
    module_source = release / "modules/sample.py"
    original_resolve = Path.resolve

    def fake_resolve(path: Path, *args, **kwargs):
        if path == module_source:
            return tmp_path / "outside.py"
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fake_resolve)
    with pytest.raises(ValueError, match="escapes the release root"):
        load_manifest(release)


def test_validate_name_uses_artifact_label_and_reserved_command_names() -> None:
    assert validate_name("config-diff") == "config-diff"
    with pytest.raises(ValueError, match="invalid artifact name"):
        validate_name("config_diff")
    for name in ["atlas", "artifact-runner"]:
        with pytest.raises(ValueError, match="reserved command name"):
            validate_name(name, kind="command")


@pytest.mark.parametrize("version", [".", "..", "a/b", r"a\\b"])
def test_read_version_rejects_unsafe_values(tmp_path: Path, version: str) -> None:
    release = tmp_path / "release"
    release.mkdir()
    (release / "VERSION").write_text(version + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid release version"):
        read_version(release)


def test_read_version_and_release_validation_reject_symlinks(tmp_path: Path) -> None:
    version_target = tmp_path / "VERSION"
    version_target.write_text("1.0.0\n", encoding="utf-8")
    release = tmp_path / "release"
    release.mkdir()
    (release / "VERSION").symlink_to(version_target)
    with pytest.raises(ValueError, match="missing VERSION file"):
        read_version(release)

    real_release = _release(tmp_path / "real-release")
    linked_release = tmp_path / "linked-release"
    linked_release.symlink_to(real_release, target_is_directory=True)
    with pytest.raises(ValueError, match="release directory not found"):
        validate_release(linked_release)


def test_validate_release_requires_manifest_and_rejects_any_symlink(tmp_path: Path) -> None:
    missing_manifest = tmp_path / "missing-manifest"
    missing_manifest.mkdir()
    (missing_manifest / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    with pytest.raises(FileNotFoundError, match=r"release\.yml"):
        validate_release(missing_manifest)

    release = _release(tmp_path / "release")
    target = release / "asset.txt"
    target.write_text("asset\n", encoding="utf-8")
    (release / "linked-asset").symlink_to(target)
    with pytest.raises(ValueError, match="symlink is not allowed in release"):
        validate_release(release)


def test_active_releases_requires_manifest_name_to_match_link(tmp_path: Path) -> None:
    release = _release(tmp_path / "release")
    current = tmp_path / "current"
    current.mkdir()
    (current / "other").symlink_to(release, target_is_directory=True)

    with pytest.raises(ValueError, match="active release name mismatch: other != sample"):
        active_releases(current)


def test_command_index_uses_only_manifest_declarations(tmp_path: Path) -> None:
    release = _release(
        tmp_path / "release",
        command_files=("sample.py", "implicit.py"),
    )
    current = tmp_path / "current"
    current.mkdir()
    (current / "sample").symlink_to(release, target_is_directory=True)

    assert list(command_index(current)) == ["sample"]
