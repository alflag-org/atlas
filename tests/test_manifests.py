from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from atlas.catalog import (
    active_releases,
    command_index,
    release_index,
    resolve_command,
    resolve_job,
    resolve_service,
)
from atlas.config import load_config
from atlas.files import remove_path
from atlas.launchers import (
    ensure_artifact_runner,
    ensure_atlas_launcher,
    regenerate_shims,
    sync_atlas_core,
)
from atlas.manifests import load_manifest, validate_name
from atlas.releases import (
    install_release,
    read_version,
    reversible_release_install,
    validate_release,
)
from atlas.yamlutil import dump_yaml_file, load_yaml_file


def _read_manifest(root: Path) -> dict[str, object]:
    value = yaml.safe_load((root / "release.yml").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _write_manifest(root: Path, value: object) -> None:
    (root / "release.yml").write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")


def test_load_manifest_models_every_artifact(release_factory) -> None:
    root = release_factory(
        name="operations",
        commands=("config-show",),
        jobs=("state-collect",),
        timeout=30,
        service="state-collect",
    )

    manifest = load_manifest(root)

    assert manifest.name == "operations"
    assert manifest.commands["config-show"].entrypoint == (root / "commands/config-show.py").resolve()
    assert manifest.jobs["state-collect"].default_timeout_seconds == 30
    service = manifest.services["state-collect"]
    assert service.job == "state-collect"
    assert service.command is None
    assert service.systemd.timer == (root / "init/systemd/state-collect.timer").resolve()


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("Bad", "release"),
        ("bad_name", "command"),
        ("bad--name", "job"),
        ("", "service"),
    ],
)
def test_validate_name_rejects_non_contract_names(name: str, kind: str) -> None:
    with pytest.raises(ValueError, match=f"invalid {kind} name"):
        validate_name(name, kind=kind)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda raw: raw.update(extra=True), "release.yml has unknown key"),
        (lambda raw: raw.update(schema="atlas.release/v2"), "unsupported release schema"),
        (lambda raw: raw.update(name="Bad"), "invalid release name"),
        (lambda raw: raw.update(commands=[]), "commands must be a mapping"),
        (
            lambda raw: raw["commands"].update(
                {"bad_name": {"runtime": "python", "entrypoint": "commands/sample-show.py"}}
            ),
            "invalid command name",
        ),
        (
            lambda raw: raw["commands"]["sample-show"].update(extra=True),
            "commands.sample-show has unknown key",
        ),
        (
            lambda raw: raw["commands"]["sample-show"].update(runtime="shell"),
            "runtime is unsupported",
        ),
        (
            lambda raw: raw["commands"]["sample-show"].update(entrypoint="../outside.py"),
            "must be a relative path",
        ),
        (
            lambda raw: raw["commands"]["sample-show"].update(entrypoint="commands/missing.py"),
            "entrypoint not found",
        ),
        (
            lambda raw: raw["jobs"].update(
                {
                    "sample-show": {
                        "runtime": "python",
                        "entrypoint": "commands/sample-show.py",
                    }
                }
            ),
            "command and job names overlap",
        ),
    ],
)
def test_load_manifest_rejects_invalid_top_level_and_executables(
    release_factory,
    mutate,
    message: str,
) -> None:
    root = release_factory()
    raw = _read_manifest(root)
    mutate(raw)
    _write_manifest(root, raw)

    with pytest.raises(ValueError, match=message):
        load_manifest(root)


@pytest.mark.parametrize("timeout", [True, 0, -1, "30"])
def test_load_manifest_rejects_invalid_job_timeout(release_factory, timeout: object) -> None:
    root = release_factory(jobs=("collect",))
    raw = _read_manifest(root)
    raw["jobs"]["collect"]["default_timeout_seconds"] = timeout
    _write_manifest(root, raw)

    with pytest.raises(ValueError, match="must be a positive integer"):
        load_manifest(root)


def test_load_manifest_rejects_symlink_and_wrong_suffix(release_factory, tmp_path: Path) -> None:
    root = release_factory()
    command = root / "commands/sample-show.py"
    command.unlink()
    command.symlink_to(tmp_path / "missing.py")
    with pytest.raises(ValueError, match="must not be a symlink"):
        load_manifest(root)

    command.unlink()
    command.write_text("print('ok')\n", encoding="utf-8")
    raw = _read_manifest(root)
    raw["commands"]["sample-show"]["entrypoint"] = "VERSION"
    _write_manifest(root, raw)
    with pytest.raises(ValueError, match=r"must end with \.py"):
        load_manifest(root)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda raw: raw["services"]["refresh"].update(command="sample-show"),
            "exactly one command or job",
        ),
        (
            lambda raw: raw["services"]["refresh"].update(job="missing"),
            "references an unknown job",
        ),
        (
            lambda raw: raw["services"]["refresh"].update(extra=True),
            "has unknown key",
        ),
        (
            lambda raw: raw["services"]["refresh"].update(init=[]),
            "init must be a mapping",
        ),
        (
            lambda raw: raw["services"]["refresh"]["init"].update(openrc={}),
            "init has unknown key",
        ),
        (
            lambda raw: raw["services"]["refresh"]["init"]["systemd"].update(extra=True),
            "systemd has unknown key",
        ),
        (
            lambda raw: raw["services"]["refresh"]["init"]["systemd"].update(timer=1),
            "timer must be a non-empty string",
        ),
    ],
)
def test_load_manifest_rejects_invalid_services(release_factory, mutate, message: str) -> None:
    root = release_factory(jobs=("collect",), service="refresh")
    raw = _read_manifest(root)
    mutate(raw)
    _write_manifest(root, raw)

    with pytest.raises(ValueError, match=message):
        load_manifest(root)


def test_release_validation_and_catalog(release_factory, tmp_path: Path) -> None:
    first = release_factory(name="first", version="1.2.3", commands=("shared-command",))
    second = release_factory(name="second", commands=("second-show",), jobs=("collect",), service="collect")
    releases = tmp_path / "releases"
    current = tmp_path / "current"

    assert read_version(first) == "1.2.3"
    assert validate_release(first).manifest.name == "first"
    first_target = install_release(first, releases, current)
    second_target = install_release(second, releases, current)

    assert first_target == releases / "first/1.2.3"
    assert second_target == releases / "second/1.0.0"
    assert list(release_index(current)) == ["first", "second"]
    assert resolve_command(current, "shared-command").release.name == "first"
    assert resolve_job(current, "second", "collect").artifact_type == "job"
    assert resolve_service(current, "second", "collect").service.job == "collect"

    with pytest.raises(ValueError, match="unknown command"):
        resolve_command(current, "missing")
    with pytest.raises(ValueError, match="unknown release"):
        resolve_job(current, "missing", "collect")
    with pytest.raises(ValueError, match="unknown job"):
        resolve_job(current, "second", "missing")
    with pytest.raises(ValueError, match="unknown service"):
        resolve_service(current, "second", "missing")
    with pytest.raises(ValueError, match="unknown release"):
        resolve_service(current, "missing", "collect")


def test_catalog_fails_closed_on_collision_and_bad_current_entries(release_factory, tmp_path: Path) -> None:
    first = release_factory(name="first", commands=("same-command",))
    second = release_factory(name="second", commands=("same-command",))
    releases = tmp_path / "releases"
    current = tmp_path / "current"
    install_release(first, releases, current)
    install_release(second, releases, current)
    with pytest.raises(ValueError, match="command name collision"):
        command_index(current)

    broken = tmp_path / "broken-current"
    broken.write_text("bad", encoding="utf-8")
    with pytest.raises(ValueError, match="current root must be a directory"):
        active_releases(broken)
    broken.unlink()
    broken.mkdir()
    (broken / "regular").write_text("bad", encoding="utf-8")
    with pytest.raises(ValueError, match="current entry must be a symlink"):
        active_releases(broken)
    (broken / "regular").unlink()
    (broken / "missing").symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="active release target not found"):
        active_releases(broken)


def test_catalog_rejects_active_manifest_name_mismatch(release_factory, tmp_path: Path) -> None:
    source = release_factory(name="actual")
    current = tmp_path / "current"
    current.mkdir()
    (current / "different").symlink_to(source, target_is_directory=True)

    with pytest.raises(ValueError, match="active release name mismatch"):
        active_releases(current)


def test_read_version_and_release_reject_invalid_files(release_factory, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="missing VERSION"):
        read_version(tmp_path / "missing")
    root = release_factory()
    (root / "VERSION").write_text("\n", encoding="utf-8")
    with pytest.raises(ValueError, match="VERSION is empty"):
        read_version(root)
    (root / "VERSION").write_text("../bad\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid release version"):
        read_version(root)
    with pytest.raises(ValueError, match="release directory not found"):
        validate_release(tmp_path / "missing")

    root = release_factory(name="linked")
    target = root / "target"
    target.write_text("x", encoding="utf-8")
    (root / "bad-link").symlink_to(target)
    with pytest.raises(ValueError, match="symlink is not allowed"):
        validate_release(root)


def test_install_replaces_same_version_and_rejects_invalid_current(release_factory, tmp_path: Path) -> None:
    source = release_factory(name="replace")
    releases = tmp_path / "releases"
    current = tmp_path / "current"
    target = install_release(source, releases, current)
    (source / "commands/sample-show.py").write_text("print('new')\n", encoding="utf-8")
    assert install_release(source, releases, current) == target
    assert (target / "commands/sample-show.py").read_text(encoding="utf-8") == "print('new')\n"
    assert not list(target.parent.glob("*.tmp.*"))
    assert not list(target.parent.glob("*.bak.*"))

    invalid_current = tmp_path / "invalid-current"
    invalid_current.write_text("bad", encoding="utf-8")
    with pytest.raises(ValueError, match="current root must be a directory"):
        install_release(source, releases, invalid_current)


def test_reversible_release_install_validates_and_restores_current_entry(
    release_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = release_factory(name="sample", version="1.0.0")
    releases = tmp_path / "releases"
    current = tmp_path / "current"
    old_target = install_release(old, releases, current)
    old_content = (old_target / "commands/sample-show.py").read_text(encoding="utf-8")
    new = release_factory(name="sample", version="1.0.0")
    (new / "commands/sample-show.py").write_text("print('new')\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="after activation"):
        with reversible_release_install(new, releases, current):
            raise RuntimeError("after activation")
    assert (current / "sample").resolve() == old_target
    assert (old_target / "commands/sample-show.py").read_text(encoding="utf-8") == old_content
    assert not list(old_target.parent.glob("*.bak.*"))

    (current / "sample").unlink()
    (current / "sample").write_text("bad", encoding="utf-8")
    with pytest.raises(ValueError, match="current entry must be a symlink"):
        install_release(new, releases, current)
    (current / "sample").unlink()
    (current / "sample").symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="active release target not found"):
        install_release(new, releases, current)

    (current / "sample").unlink()
    (current / "sample").symlink_to(old_target, target_is_directory=True)
    def fail_target_remove(path: Path) -> None:
        if path == old_target:
            raise OSError("rollback remove failed")
        remove_path(path)

    with pytest.raises(RuntimeError, match="rollback failed; recovery path"):
        with reversible_release_install(new, releases, current):
            monkeypatch.setattr("atlas.releases.remove_path", fail_target_remove)
            raise ValueError("refresh failed")


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_release_install_rejects_non_directory_version_target(
    release_factory,
    tmp_path: Path,
    kind: str,
) -> None:
    source = release_factory(name="sample")
    target = tmp_path / "releases/sample/1.0.0"
    target.parent.mkdir(parents=True)
    if kind == "file":
        target.write_text("bad", encoding="utf-8")
    else:
        target.symlink_to(tmp_path / "missing", target_is_directory=True)
    with pytest.raises(ValueError, match="release target must be a directory"):
        install_release(source, tmp_path / "releases", tmp_path / "current")


@pytest.mark.parametrize("existing", [False, True])
def test_release_directory_replacement_rolls_back_rename_failure(
    release_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
) -> None:
    source = release_factory(name="sample")
    releases = tmp_path / "releases"
    current = tmp_path / "current"
    target = releases / "sample/1.0.0"
    if existing:
        install_release(source, releases, current)
    original = Path.rename

    def fail_staging(self: Path, destination: Path):
        if ".tmp." in self.name:
            raise RuntimeError("rename failed")
        return original(self, destination)

    monkeypatch.setattr(Path, "rename", fail_staging)
    with pytest.raises(RuntimeError, match="rename failed"):
        install_release(source, releases, current)
    assert target.exists() is existing


def test_release_directory_retains_backup_when_restore_fails(
    release_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = release_factory(name="sample")
    releases = tmp_path / "releases"
    current = tmp_path / "current"
    target = install_release(source, releases, current)
    original_content = (target / "commands/sample-show.py").read_text(encoding="utf-8")
    (source / "commands/sample-show.py").write_text("print('new')\n", encoding="utf-8")
    original = Path.rename

    def fail_install_and_restore(self: Path, destination: Path):
        if ".tmp." in self.name:
            raise RuntimeError("install failed")
        if ".bak." in self.name:
            raise RuntimeError("restore failed")
        return original(self, destination)

    monkeypatch.setattr(Path, "rename", fail_install_and_restore)
    with pytest.raises(RuntimeError, match="backup retained at"):
        install_release(source, releases, current)
    backup = next(target.parent.glob("*.bak.*"))
    assert (backup / "commands/sample-show.py").read_text(encoding="utf-8") == original_content


def test_config_is_strict_and_has_no_legacy_scripts_section(tmp_path: Path) -> None:
    path = tmp_path / "config.yml"
    path.write_text(
        "runtime:\n"
        "  python:\n"
        "    version: '3.12.3'\n"
        "releases:\n"
        "  operations:\n"
        "    source: /release\n"
        "  disabled:\n"
        "    source: /disabled\n"
        "    enabled: false\n",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.runtime.python_version == "3.12.3"
    assert config.releases["operations"].enabled is True
    assert config.releases["disabled"].enabled is False

    invalid_bodies = [
        ("[]\n", "config.yml must be a mapping"),
        ("runtime: {}\nreleases: {}\n", "runtime.python must be a mapping"),
        (
            "runtime:\n  python:\n    version: ''\nreleases: {}\n",
            "runtime.python.version is required",
        ),
        (
            "runtime:\n  python:\n    version: '3.12'\nscripts: {}\nreleases: {}\n",
            "config.yml has unknown key",
        ),
        (
            "runtime:\n  python:\n    version: '3.12'\nreleases:\n  bad_name:\n    source: x\n",
            "invalid release name",
        ),
        (
            "runtime:\n  python:\n    version: '3.12'\nreleases:\n  sample:\n    source: ''\n",
            "source is required",
        ),
        (
            "runtime:\n  python:\n    version: '3.12'\nreleases:\n  sample:\n    source: x\n    enabled: 1\n",
            "enabled must be a boolean",
        ),
    ]
    for body, message in invalid_bodies:
        path.write_text(body, encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            load_config(path)

    path.write_text(
        "runtime:\n  python:\n    version: '3.12'\nreleases:\n  1:\n    source: x\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="release name must be a string"):
        load_config(path)


def test_manifest_rejects_missing_strings_numeric_names_and_parent_symlink(
    release_factory,
    tmp_path: Path,
) -> None:
    root = release_factory()
    raw = _read_manifest(root)
    raw.pop("schema")
    _write_manifest(root, raw)
    with pytest.raises(ValueError, match="release.yml.schema is required"):
        load_manifest(root)

    root = release_factory()
    raw = _read_manifest(root)
    raw["commands"][1] = raw["commands"].pop("sample-show")
    _write_manifest(root, raw)
    with pytest.raises(ValueError, match="command name must be a string"):
        load_manifest(root)

    root = release_factory(jobs=("collect",), service="refresh")
    raw = _read_manifest(root)
    raw["services"][1] = raw["services"].pop("refresh")
    _write_manifest(root, raw)
    with pytest.raises(ValueError, match="service name must be a string"):
        load_manifest(root)

    root = release_factory()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sample-show.py").write_text("print('x')\n", encoding="utf-8")
    for item in (root / "commands").iterdir():
        item.unlink()
    (root / "commands").rmdir()
    (root / "commands").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes the release root"):
        load_manifest(root)


def test_manifest_supports_command_service_and_service_without_timer(release_factory) -> None:
    root = release_factory(commands=("sample-show",), jobs=("collect",), service="refresh")
    raw = _read_manifest(root)
    service = raw["services"]["refresh"]
    service.pop("job")
    service["command"] = "sample-show"
    service["init"]["systemd"].pop("timer")
    _write_manifest(root, raw)
    manifest = load_manifest(root)
    assert manifest.services["refresh"].command == "sample-show"
    assert manifest.services["refresh"].job is None
    assert manifest.services["refresh"].systemd.timer is None

    raw = _read_manifest(root)
    raw["services"]["refresh"]["command"] = "missing"
    _write_manifest(root, raw)
    with pytest.raises(ValueError, match="references an unknown command"):
        load_manifest(root)


def test_yaml_is_duplicate_safe_and_dump_creates_parent(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.yml"
    path.write_text("a: 1\na: 2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate YAML key"):
        load_yaml_file(path)
    with pytest.raises(FileNotFoundError, match="file not found"):
        load_yaml_file(tmp_path / "missing.yml")

    output = tmp_path / "nested/output.yml"
    dump_yaml_file(output, {"a": 1})
    assert load_yaml_file(output) == {"a": 1}


def test_launchers_sync_core_and_generate_command_only_shims(
    release_factory,
    tmp_path: Path,
) -> None:
    source = release_factory(name="sample", commands=("sample-show",), jobs=("hidden-job",))
    current = tmp_path / "current"
    install_release(source, tmp_path / "releases", current)
    home = tmp_path / "home"
    atlas_bin = home / "bin/atlas"
    runner = home / "bin/artifact-runner"
    ensure_atlas_launcher(atlas_bin)
    ensure_artifact_runner(runner, atlas_bin)
    sync_atlas_core(home)
    shims = home / "shims"
    (shims / "manual").mkdir(parents=True)
    (shims / "stale").write_text("stale", encoding="utf-8")

    assert regenerate_shims(current, shims, runner) == ["sample-show"]
    assert (shims / "sample-show").resolve() == runner
    assert not (shims / "hidden-job").exists()
    assert not (shims / "stale").exists()
    assert (shims / "manual").is_dir()
    assert (home / "lib/python/atlas_core/context.py").is_file()
    assert '-m atlas.cli "$@"' in atlas_bin.read_text(encoding="utf-8")
    assert f'exec "{atlas_bin}" run' in runner.read_text(encoding="utf-8")


def test_regenerate_shims_rejects_directory_at_command_path(release_factory, tmp_path: Path) -> None:
    source = release_factory()
    current = tmp_path / "current"
    install_release(source, tmp_path / "releases", current)
    shims = tmp_path / "shims"
    (shims / "sample-show").mkdir(parents=True)
    stale = shims / "stale"
    stale.write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="shim path is a directory"):
        regenerate_shims(current, shims, tmp_path / "artifact-runner")
    assert stale.read_text(encoding="utf-8") == "keep"

    invalid_shims = tmp_path / "invalid-shims"
    invalid_shims.write_text("bad", encoding="utf-8")
    with pytest.raises(ValueError, match="shims path must be a directory"):
        regenerate_shims(current, invalid_shims, tmp_path / "artifact-runner")

    linked_shims = tmp_path / "linked-shims"
    linked_shims.symlink_to(shims, target_is_directory=True)
    with pytest.raises(ValueError, match="shims path must be a directory"):
        regenerate_shims(current, linked_shims, tmp_path / "artifact-runner")

    dangling_shims = tmp_path / "dangling-shims"
    dangling_shims.symlink_to(tmp_path / "missing", target_is_directory=True)
    with pytest.raises(ValueError, match="shims path must be a directory"):
        regenerate_shims(current, dangling_shims, tmp_path / "artifact-runner")
