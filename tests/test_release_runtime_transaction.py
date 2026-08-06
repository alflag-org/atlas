from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from atlas.cli import main
from atlas.releases import install_release, validate_release


def _release(path: Path, *, name: str = "default", version: str = "1.0.0") -> Path:
    modules = path / "modules"
    modules.mkdir(parents=True)
    (path / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    (modules / "sample.py").write_text(
        "def main(argv: list[str] | None = None) -> int:\n"
        "    return 0\n",
        encoding="utf-8",
    )
    (path / "release.yml").write_text(
        "schema: atlas.release/v1\n"
        f"name: {name}\n"
        "commands:\n"
        "  sample:\n"
        "    target: sample:main\n",
        encoding="utf-8",
    )
    return path


def _wheel(path: Path, package: str, version: str, value: str) -> Path:
    distribution = f"{package}-{version}"
    dist_info = f"{distribution}.dist-info"
    wheel = path / f"{distribution}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(f"{package}/__init__.py", f"VALUE = {value!r}\n")
        archive.writestr(
            f"{dist_info}/METADATA",
            f"Metadata-Version: 2.1\nName: {package}\nVersion: {version}\n",
        )
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: atlas-test\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(
            f"{dist_info}/RECORD",
            f"{package}/__init__.py,,\n"
            f"{dist_info}/METADATA,,\n"
            f"{dist_info}/WHEEL,,\n"
            f"{dist_info}/RECORD,,\n",
        )
    return wheel


def _install_atlas_wheel(python: Path, destination: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    distribution = "atlas-2.0.0"
    dist_info = f"{distribution}.dist-info"
    support_requirement = (root / "src/atlas/support-requirements.txt").read_text(
        encoding="utf-8"
    ).strip()
    wheel = destination / f"{distribution}-py3-none-any.whl"
    files: list[str] = []
    with zipfile.ZipFile(wheel, "w") as archive:
        for package in ("atlas", "atlas_core"):
            package_root = root / "src" / package
            for source in sorted(package_root.rglob("*.py")):
                relative = source.relative_to(root / "src").as_posix()
                archive.write(source, relative)
                files.append(relative)
        support_requirements = root / "src/atlas/support-requirements.txt"
        archive.write(
            support_requirements,
            support_requirements.relative_to(root / "src").as_posix(),
        )
        files.append("atlas/support-requirements.txt")
        archive.writestr(
            f"{dist_info}/METADATA",
            "Metadata-Version: 2.1\n"
            "Name: atlas\n"
            "Version: 2.0.0\n"
            "Requires-Python: >=3.11\n"
            f"Requires-Dist: {support_requirement}\n",
        )
        files.append(f"{dist_info}/METADATA")
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\n"
            "Generator: atlas-test\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n",
        )
        files.append(f"{dist_info}/WHEEL")
        archive.writestr(
            f"{dist_info}/RECORD",
            "".join(f"{file},,\n" for file in [*files, f"{dist_info}/RECORD"]),
        )
    subprocess.run(
        [str(python), "-m", "pip", "install", "--no-deps", "--no-index", str(wheel)],
        check=True,
        capture_output=True,
        text=True,
    )


def _runtime_python(runtime_root: Path) -> Path:
    return runtime_root / "python/envs/scripts/bin/python"


def _runtime_value(runtime_root: Path, package: str) -> str:
    completed = subprocess.run(
        [
            str(_runtime_python(runtime_root)),
            "-c",
            f"import {package}; print({package}.VALUE)",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _install_with_current_bootstrap(
    source: Path,
    releases_root: Path,
    current_root: Path,
    runtime_root: Path,
) -> Path:
    return install_release(
        source,
        releases_root,
        current_root,
        runtime_root=runtime_root,
        python_version=f"{sys.version_info.major}.{sys.version_info.minor}",
        runtime_python=Path(sys.executable),
    )


def test_fresh_host_candidate_runtime_installs_release_dependency(tmp_path: Path) -> None:
    dependency = _wheel(tmp_path, "fresh_dependency", "1.0.0", "fresh")
    source = _release(tmp_path / "source")
    (source / "requirements.txt").write_text(f"{dependency}\n", encoding="utf-8")
    (source / "modules/sample.py").write_text(
        "from fresh_dependency import VALUE\n"
        "def main(argv: list[str] | None = None) -> int:\n"
        "    return 0 if VALUE == 'fresh' else 1\n",
        encoding="utf-8",
    )
    runtime = tmp_path / "runtime"

    target = _install_with_current_bootstrap(
        source,
        tmp_path / "releases",
        tmp_path / "current",
        runtime,
    )

    assert (tmp_path / "current/default").resolve() == target
    assert _runtime_value(runtime, "fresh_dependency") == "fresh"

    updated_dependency = _wheel(tmp_path, "fresh_dependency", "2.0.0", "updated")
    updated = _release(tmp_path / "updated", version="2.0.0")
    (updated / "requirements.txt").write_text(
        f"{updated_dependency}\n",
        encoding="utf-8",
    )
    (updated / "modules/sample.py").write_text(
        "from fresh_dependency import VALUE\n"
        "def main(argv: list[str] | None = None) -> int:\n"
        "    return 0 if VALUE == 'updated' else 1\n",
        encoding="utf-8",
    )

    updated_target = _install_with_current_bootstrap(
        updated,
        tmp_path / "releases",
        tmp_path / "current",
        runtime,
    )

    assert (tmp_path / "current/default").resolve() == updated_target
    assert _runtime_value(runtime, "fresh_dependency") == "updated"


def test_atlas_installed_in_outer_venv_does_not_leak_outer_dependency(
    tmp_path: Path,
) -> None:
    dependency = _wheel(tmp_path, "outer_only_dependency", "1.0.0", "outer")
    outer = tmp_path / "outer"
    subprocess.run(
        [sys.executable, "-m", "venv", "--system-site-packages", str(outer)],
        check=True,
    )
    outer_python = outer / "bin/python"
    subprocess.run(
        [str(outer_python), "-m", "pip", "install", str(dependency)],
        check=True,
        capture_output=True,
        text=True,
    )
    _install_atlas_wheel(outer_python, tmp_path)
    source = _release(tmp_path / "source")
    (source / "modules/sample.py").write_text(
        "import outer_only_dependency\n"
        "def main(argv: list[str] | None = None) -> int:\n"
        "    return 0\n",
        encoding="utf-8",
    )

    home = tmp_path / "home"
    etc = tmp_path / "etc"
    var = tmp_path / "var"
    etc.mkdir()
    (etc / "config.yml").write_text(
        "runtime:\n"
        "  python:\n"
        "    version: 'outer-bootstrap'\n"
        "releases: {}\n",
        encoding="utf-8",
    )
    pyenv = tmp_path / "pyenv-bin/pyenv"
    pyenv.parent.mkdir()
    pyenv.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "case \"${1:-}\" in\n"
        "  install) exit 0 ;;\n"
        f"  prefix) printf '%s\\n' '{outer}' ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    pyenv.chmod(0o755)
    environment = os.environ.copy()
    environment.update(
        {
            "ATLAS_HOME": str(home),
            "ATLAS_ETC_DIR": str(etc),
            "ATLAS_VAR_DIR": str(var),
            "ATLAS_RUNTIME_DIR": str(home / "runtime"),
            "PATH": f"{pyenv.parent}{os.pathsep}{environment['PATH']}",
        }
    )
    environment.pop("PYTHONPATH", None)
    installed = subprocess.run(
        [str(outer_python), "-c", "import atlas; print(atlas.__file__)"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert Path(installed.stdout.strip()).is_relative_to(outer)
    completed = subprocess.run(
        [str(outer_python), "-m", "atlas.cli", "release", "install", str(source)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 2
    assert "release target validation failed" in completed.stderr

    assert not (home / "current/default").exists()
    assert not list((home / "releases/default").glob("1.0.0-*"))


def test_candidate_runtime_ignores_hostile_pip_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "marker"
    marker.write_text("untouched", encoding="utf-8")
    pip_config = tmp_path / "pip.conf"
    pip_config.write_text(
        "[global]\n"
        f"target = {outside}\n"
        "no-index = true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PIP_CONFIG_FILE", str(pip_config))
    monkeypatch.setenv("PIP_TARGET", str(outside))
    monkeypatch.setenv("PIP_PREFIX", str(outside / "prefix"))
    monkeypatch.setenv("PIP_USER", "1")
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.setenv("PIP_INDEX_URL", "file:///outside/index")
    monkeypatch.setenv("TMPDIR", str(outside / "tmp"))

    source = _release(tmp_path / "source")
    (source / "modules/sample.py").write_text(
        "import yaml\n"
        "def main(argv: list[str] | None = None) -> int:\n"
        "    return 0 if yaml.__version__ == '6.0.3' else 1\n",
        encoding="utf-8",
    )
    runtime = tmp_path / "runtime"

    install_release(
        source,
        tmp_path / "releases",
        tmp_path / "current",
        runtime_root=runtime,
        python_version=f"{sys.version_info.major}.{sys.version_info.minor}",
        runtime_python=Path(sys.executable),
    )

    assert marker.read_text(encoding="utf-8") == "untouched"
    assert list(outside.iterdir()) == [marker]
    completed = subprocess.run(
        [
            str(_runtime_python(runtime)),
            "-c",
            "import yaml; print(yaml.__version__)",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.strip() == "6.0.3"


def test_failed_candidate_preserves_active_release_and_runtime(tmp_path: Path) -> None:
    dependency = _wheel(tmp_path, "preserved_dependency", "1.0.0", "old")
    source = _release(tmp_path / "old")
    (source / "requirements.txt").write_text(f"{dependency}\n", encoding="utf-8")
    (source / "modules/sample.py").write_text(
        "from preserved_dependency import VALUE\n"
        "def main(argv: list[str] | None = None) -> int:\n"
        "    return 0 if VALUE == 'old' else 1\n",
        encoding="utf-8",
    )
    releases = tmp_path / "releases"
    current = tmp_path / "current"
    runtime = tmp_path / "runtime"
    old_target = _install_with_current_bootstrap(source, releases, current, runtime)
    old_runtime = _runtime_python(runtime).resolve()

    failed = _release(tmp_path / "failed", version="2.0.0")
    (failed / "requirements.txt").write_text(
        str(tmp_path / "missing-dependency.whl") + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"command failed: .* -m pip install"):
        _install_with_current_bootstrap(failed, releases, current, runtime)

    assert (current / "default").resolve() == old_target
    assert _runtime_python(runtime).resolve() == old_runtime
    assert _runtime_value(runtime, "preserved_dependency") == "old"
    assert not list((releases / "default").glob("2.0.0-*"))


def test_runtime_replacement_keeps_previous_generation_when_gc_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    old = _release(tmp_path / "old", version="1.0.0")
    releases = tmp_path / "releases"
    current = tmp_path / "current"
    runtime = tmp_path / "runtime"
    _install_with_current_bootstrap(old, releases, current, runtime)
    old_generation = (runtime / "python/envs/scripts").resolve()

    new = _release(tmp_path / "new", version="2.0.0")
    _install_with_current_bootstrap(new, releases, current, runtime)
    active_generation = (runtime / "python/envs/scripts").resolve()
    assert active_generation != old_generation
    assert old_generation.is_dir()

    def fail_old_cleanup(path: Path) -> None:
        if path == old_generation:
            raise OSError("simulated old-generation cleanup failure")
        path.unlink() if path.is_file() or path.is_symlink() else shutil.rmtree(path)

    monkeypatch.setattr("atlas.generations.remove_path", fail_old_cleanup)
    from atlas.generations import collect_generation_garbage

    collect_generation_garbage(
        old_generation.parent,
        runtime / "python/envs/scripts",
        label="runtime generation",
    )
    assert (runtime / "python/envs/scripts").resolve() == active_generation
    assert old_generation.is_dir()


def test_conflicting_active_release_requirements_preserve_previous_runtime(
    tmp_path: Path,
) -> None:
    first_dependency = _wheel(tmp_path, "conflict_dependency", "1.0.0", "one")
    second_dependency = _wheel(tmp_path, "conflict_dependency", "2.0.0", "two")
    first = _release(tmp_path / "first", name="first")
    second = _release(tmp_path / "second", name="second")
    (first / "requirements.txt").write_text(f"{first_dependency}\n", encoding="utf-8")
    (second / "requirements.txt").write_text(f"{second_dependency}\n", encoding="utf-8")
    releases = tmp_path / "releases"
    current = tmp_path / "current"
    runtime = tmp_path / "runtime"
    first_target = _install_with_current_bootstrap(first, releases, current, runtime)
    old_runtime = _runtime_python(runtime).resolve()

    with pytest.raises(ValueError, match=r"command failed: .* -m pip install"):
        _install_with_current_bootstrap(second, releases, current, runtime)

    assert (current / "first").resolve() == first_target
    assert not (current / "second").exists()
    assert _runtime_python(runtime).resolve() == old_runtime
    assert _runtime_value(runtime, "conflict_dependency") == "one"
    assert not list((releases / "second").glob("1.0.0-*"))


def test_existing_matching_snapshot_is_callable_validated_before_activation(
    tmp_path: Path,
) -> None:
    source = _release(tmp_path / "source")
    (source / "modules/sample.py").write_text(
        "def main(argv, required):\n"
        "    return 0\n",
        encoding="utf-8",
    )
    validated = validate_release(source, validate_targets=False)
    target = tmp_path / "releases/default" / (
        f"{validated.version}-{validated.content_digest}"
    )
    target.parent.mkdir(parents=True)
    shutil.copytree(source, target)
    for item in [*target.rglob("*"), target]:
        item.chmod(stat.S_IMODE(item.stat().st_mode) & ~0o222)

    with pytest.raises(ValueError, match="release target validation failed"):
        _install_with_current_bootstrap(
            source,
            tmp_path / "releases",
            tmp_path / "current",
            tmp_path / "runtime",
        )

    assert not (tmp_path / "current/default").exists()
    assert target.is_dir()
    assert not (tmp_path / "runtime/python/envs/scripts").exists()


def test_existing_invalid_snapshot_does_not_publish_cli_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys,
) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True)
    (etc / "config.yml").write_text(
        "runtime:\n"
        "  python:\n"
        f"    version: '{sys.version_info.major}.{sys.version_info.minor}'\n"
        "releases: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ATLAS_HOME", str(home))
    monkeypatch.setenv("ATLAS_ETC_DIR", str(etc))
    monkeypatch.setenv("ATLAS_VAR_DIR", str(var))
    monkeypatch.setenv("ATLAS_RUNTIME_DIR", str(home / "runtime"))
    monkeypatch.setattr(
        "atlas.runtime._ensure_pyenv_runtime",
        lambda version, env=None: Path(sys.executable),
    )

    old_source = _release(tmp_path / "old-source", version="0.9.0")
    assert main(["release", "install", str(old_source)]) == 0
    old_target = (home / "current/default").resolve()
    old_runtime = (home / "runtime/python/envs/scripts").resolve()
    old_artifacts = (home / "artifacts/current").resolve()

    source = _release(tmp_path / "source", version="1.0.0")
    (source / "modules/sample.py").write_text(
        "def main(argv, required):\n"
        "    return 0\n",
        encoding="utf-8",
    )
    validated = validate_release(source, validate_targets=False)
    target = home / "releases/default" / (
        f"{validated.version}-{validated.content_digest}"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    for item in [*target.rglob("*"), target]:
        item.chmod(stat.S_IMODE(item.stat().st_mode) & ~0o222)

    assert main(["release", "install", str(source)]) == 2
    error = capsys.readouterr().err
    assert "atlas: release target validation failed for sample:main:" in error
    assert (
        "ValueError: target callable has required positional arguments beyond argv: sample:main"
        in error
    )
    assert (home / "current/default").resolve() == old_target
    assert (home / "runtime/python/envs/scripts").resolve() == old_runtime
    assert (home / "artifacts/current").resolve() == old_artifacts
    assert target.is_dir()


def test_release_install_uses_configured_pyenv_python(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    pyenv = tmp_path / "pyenv-bin/pyenv"
    pyenv.parent.mkdir()
    prefix = Path(sys.executable).parent.parent
    pyenv.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "case \"${1:-}\" in\n"
        f"  install) [ \"${{2:-}}\" = '-s' ] && [ \"${{3:-}}\" = '{version}' ] ;;\n"
        f"  prefix) [ \"${{2:-}}\" = '{version}' ] && printf '%s\\n' '{prefix}' ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    pyenv.chmod(0o755)
    monkeypatch.setenv("PATH", f"{pyenv.parent}{os.pathsep}{os.environ['PATH']}")
    source = _release(tmp_path / "source")
    runtime = tmp_path / "runtime"
    install_release(
        source,
        tmp_path / "releases",
        tmp_path / "current",
        runtime_root=runtime,
        python_version=version,
    )

    completed = subprocess.run(
        [
            str(_runtime_python(runtime)),
            "-c",
            "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.strip() == version
