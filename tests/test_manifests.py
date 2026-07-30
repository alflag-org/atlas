from __future__ import annotations

from pathlib import Path

import pytest

from atlas.manifests import (
    CommandArtifact,
    ReleaseManifest,
    load_manifest,
    validate_name,
)
from atlas.releases import read_version, validate_release
from atlas.scriptsets import active_releases, build_command_index


def _release(
    path: Path,
    *,
    manifest: str | None = None,
    command_files: tuple[str, ...] = ("commands/sample.py",),
    version: str = "1.0.0",
) -> Path:
    path.mkdir(parents=True)
    (path / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    for command_file in command_files:
        entrypoint = path / command_file
        entrypoint.parent.mkdir(parents=True, exist_ok=True)
        entrypoint.write_text("print('ok')\n", encoding="utf-8")
    if manifest is None:
        manifest = (
            "schema: atlas.release/v1\n"
            "name: sample\n"
            "commands:\n"
            "  sample:\n"
            "    runtime: python\n"
            "    entrypoint: commands/sample.py\n"
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
            "    runtime: python\n"
            "    entrypoint: commands/config-check.py\n"
            "  inventory-show:\n"
            "    runtime: python\n"
            "    entrypoint: commands/inventory-show.py\n"
        ),
        command_files=(
            "commands/config-check.py",
            "commands/inventory-show.py",
            "commands/not-declared.py",
        ),
    )

    manifest = load_manifest(release)

    assert manifest == ReleaseManifest(
        name="operations",
        commands={
            "config-check": CommandArtifact(
                name="config-check",
                runtime="python",
                entrypoint=(release / "commands/config-check.py").resolve(),
            ),
            "inventory-show": CommandArtifact(
                name="inventory-show",
                runtime="python",
                entrypoint=(release / "commands/inventory-show.py").resolve(),
            ),
        },
    )
    assert "not-declared" not in manifest.commands


def test_load_manifest_allows_a_release_without_commands(tmp_path: Path) -> None:
    release = _release(
        tmp_path / "release",
        manifest="schema: atlas.release/v1\nname: assets-only\n",
    )

    assert load_manifest(release).commands == {}


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
            "  sample:\n    runtime: python\n    entrypoint: commands/sample.py\n    extra: true\n",
            ValueError,
            "commands.sample has unknown key: extra",
        ),
        (
            "schema: atlas.release/v1\nname: sample\ncommands:\n"
            "  sample:\n    entrypoint: commands/sample.py\n",
            ValueError,
            "commands.sample.runtime is required",
        ),
        (
            "schema: atlas.release/v1\nname: sample\ncommands:\n"
            "  sample:\n    runtime: shell\n    entrypoint: commands/sample.py\n",
            ValueError,
            "commands.sample.runtime is unsupported",
        ),
        (
            "schema: atlas.release/v1\nname: sample\ncommands:\n"
            "  sample:\n    runtime: python\n",
            ValueError,
            "commands.sample.entrypoint is required",
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
    ("entrypoint", "message"),
    [
        ("/tmp/sample.py", "must be a relative path"),
        ("../sample.py", "must be a relative path"),
        (r"commands\\sample.py", "must be a relative path"),
        ("commands/missing.py", "not found"),
        ("commands/sample.txt", r"must end with \.py"),
    ],
)
def test_load_manifest_rejects_unsafe_entrypoints(
    tmp_path: Path,
    entrypoint: str,
    message: str,
) -> None:
    release = _release(
        tmp_path / "release",
        manifest=(
            "schema: atlas.release/v1\n"
            "name: sample\n"
            "commands:\n"
            "  sample:\n"
            "    runtime: python\n"
            f"    entrypoint: {entrypoint}\n"
        ),
        command_files=("commands/sample.py", "commands/sample.txt"),
    )

    with pytest.raises(ValueError, match=message):
        load_manifest(release)


def test_load_manifest_rejects_entrypoint_and_parent_symlinks(tmp_path: Path) -> None:
    outside = tmp_path / "outside.py"
    outside.write_text("print('outside')\n", encoding="utf-8")
    release = _release(tmp_path / "release")
    (release / "commands/sample.py").unlink()
    (release / "commands/sample.py").symlink_to(outside)
    with pytest.raises(ValueError, match="must not contain a symlink"):
        load_manifest(release)

    (release / "commands").rename(release / "real-commands")
    (release / "commands").symlink_to(release / "real-commands", target_is_directory=True)
    with pytest.raises(ValueError, match="must not contain a symlink"):
        load_manifest(release)


def test_load_manifest_rejects_resolved_escape(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    release = _release(tmp_path / "release")
    entrypoint = release / "commands/sample.py"
    original_resolve = Path.resolve

    def fake_resolve(path: Path, *args, **kwargs):
        if path == entrypoint:
            return tmp_path / "outside.py"
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fake_resolve)
    with pytest.raises(ValueError, match="escapes the release root"):
        load_manifest(release)


def test_validate_name_uses_artifact_label_and_reserved_command_names() -> None:
    assert validate_name("config-diff") == "config-diff"
    with pytest.raises(ValueError, match="invalid artifact name"):
        validate_name("config_diff")
    for name in ["atlas", "artifact-runner", "script-runner"]:
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
        command_files=("commands/sample.py", "commands/implicit.py"),
    )
    current = tmp_path / "current"
    current.mkdir()
    (current / "sample").symlink_to(release, target_is_directory=True)

    assert list(build_command_index(current)) == ["sample"]
