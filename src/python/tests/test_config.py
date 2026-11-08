from __future__ import annotations

import sys
from pathlib import Path

import pytest

from atlas.config import load_config


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_python_and_native_programs(tmp_path: Path) -> None:
    root = tmp_path / "program"
    path = write(
        tmp_path / "config.yml",
        f"""runtime:
  python:
    version: "3.13"
    executable: "{sys.executable}"
programs:
  python-tool:
    root: "{root}"
    runtime:
      type: python
      version: "3.12"
      environment: python-tool
  native-tool:
    root: "{tmp_path / 'native'}"
    runtime:
      type: native
""",
    )

    config = load_config(path)

    assert config.runtime["python"].version == "3.13"
    assert config.runtime["python"].executable == Path(sys.executable)
    assert config.programs["python-tool"].runtime.version == "3.12"
    assert config.programs["native-tool"].runtime.type == "native"


def test_runtime_and_program_defaults_are_valid(tmp_path: Path) -> None:
    path = write(
        tmp_path / "config.yml",
        f"""programs:
  tool:
    root: "{tmp_path / 'tool'}"
    runtime:
      type: python
""",
    )

    config = load_config(path)

    assert config.runtime == {}
    assert config.programs["tool"].runtime.environment == "tool"
    runtime_only = write(tmp_path / "runtime-only.yml", "runtime: {}\nprograms: {}\n")
    assert load_config(runtime_only).runtime == {}


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[]\n", "config.yml must be a mapping"),
        ("runtime: []\n", "runtime must be a mapping"),
        ("runtime:\n  python: {}\n", "runtime.python.version or runtime.python.executable is required"),
        ("programs: []\n", "programs must be a mapping"),
        ("programs:\n  Bad: {}\n", "invalid program name"),
        ("programs:\n  tool:\n    root: relative\n    runtime:\n      type: native\n", "root must be an absolute path"),
        ("programs:\n  tool:\n    root: /tmp/tool\n    runtime:\n      type: other\n", "must be python, perl or native"),
        ("programs:\n  tool:\n    root: /tmp/tool\n    runtime:\n      type: python\n      environment: atlas\n", "reserved environment name"),
        ("extra: true\n", "config.yml has unknown key"),
    ],
)
def test_rejects_invalid_config(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        load_config(write(tmp_path / "config.yml", text))


def test_rejects_duplicate_venv(tmp_path: Path) -> None:
    path = write(
        tmp_path / "config.yml",
        """programs:
  one:
    root: /tmp/one
    runtime:
      type: python
      environment: shared
  two:
    root: /tmp/two
    runtime:
      type: python
      environment: shared
""",
    )

    with pytest.raises(ValueError, match="multiple programs"):
        load_config(path)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (
            "programs:\n  tool:\n    root: /tmp/tool\n    runtime:\n      type: python\n      environment:\n",
            "environment must be a non-empty string",
        ),
        (
            "programs:\n  tool:\n    root: /tmp/tool\n    runtime:\n      type: native\n      extra: true\n",
            "runtime has unknown key",
        ),
        (
            "programs:\n  tool:\n    root: /tmp/tool\n    runtime:\n      type: python\n      version:\n",
            "version must be a non-empty string",
        ),
    ],
)
def test_rejects_missing_and_wrong_program_values(
    tmp_path: Path,
    text: str,
    message: str,
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        load_config(write(tmp_path / "config.yml", text))


def test_rejects_non_string_program_name_and_required_fields(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="program name"):
        load_config(write(tmp_path / "config.yml", "programs:\n  1: {}\n"))
    with pytest.raises(ValueError, match="root is required"):
        load_config(
            write(
                tmp_path / "missing-root.yml",
                "programs:\n  tool:\n    runtime:\n      type: native\n",
            )
        )


def test_language_environments_are_distinct_and_old_keys_are_rejected(tmp_path):
    text = '''runtime:
  perl:
    version: '5.40.0'
programs:
  python-tool:
    root: /srv/python-tool
    runtime:
      type: python
      environment: shared
  perl-tool:
    root: /srv/perl-tool
    runtime:
      type: perl
      version: '5.42.0'
      environment: shared
'''
    config = load_config(write(tmp_path / 'config.yml', text))
    assert config.runtime['perl'].version == '5.40.0'
    assert config.programs['perl-tool'].runtime.version == '5.42.0'
    for old_key in ('python', 'venv'):
        old = f'programs:\n  tool:\n    root: /srv/tool\n    runtime:\n      type: python\n      {old_key}: old\n'
        with pytest.raises(ValueError, match='unknown key'):
            load_config(write(tmp_path / 'old.yml', old))


@pytest.mark.parametrize("version", ["--help", "../escape", ".", "..", "/absolute"])
def test_runtime_versions_cannot_be_paths_or_manager_options(tmp_path, version):
    for text in (
        f"runtime:\n  perl:\n    version: '{version}'\n",
        f"programs:\n  tool:\n    root: /srv/tool\n    runtime:\n      type: perl\n      version: '{version}'\n",
    ):
        with pytest.raises(ValueError, match="interpreter version or name"):
            load_config(write(tmp_path / "config.yml", text))
