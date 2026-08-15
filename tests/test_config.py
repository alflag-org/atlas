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
      python: "3.12"
      venv: python-tool
  native-tool:
    root: "{tmp_path / 'native'}"
    runtime:
      type: native
""",
    )

    config = load_config(path)

    assert config.runtime.python_version == "3.13"
    assert config.runtime.executable == Path(sys.executable)
    assert config.programs["python-tool"].runtime.python_version == "3.12"
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

    assert config.runtime.python_version is None
    assert config.programs["tool"].runtime.venv == "tool"
    runtime_only = write(tmp_path / "runtime-only.yml", "runtime: {}\nprograms: {}\n")
    assert load_config(runtime_only).runtime.python_version is None


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[]\n", "config.yml must be a mapping"),
        ("runtime: []\n", "runtime must be a mapping"),
        ("runtime:\n  python: {}\n", "runtime.python.version or runtime.python.executable is required"),
        ("programs: []\n", "programs must be a mapping"),
        ("programs:\n  Bad: {}\n", "invalid program name"),
        ("programs:\n  tool:\n    root: relative\n    runtime:\n      type: native\n", "root must be an absolute path"),
        ("programs:\n  tool:\n    root: /tmp/tool\n    runtime:\n      type: other\n", "must be python or native"),
        ("programs:\n  tool:\n    root: /tmp/tool\n    runtime:\n      type: python\n      venv: atlas\n", "reserved venv name"),
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
      venv: shared
  two:
    root: /tmp/two
    runtime:
      type: python
      venv: shared
""",
    )

    with pytest.raises(ValueError, match="multiple programs"):
        load_config(path)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (
            "programs:\n  tool:\n    root: /tmp/tool\n    runtime:\n      type: python\n      venv:\n",
            "venv must be a non-empty string",
        ),
        (
            "programs:\n  tool:\n    root: /tmp/tool\n    runtime:\n      type: native\n      extra: true\n",
            "runtime has unknown key",
        ),
        (
            "programs:\n  tool:\n    root: /tmp/tool\n    runtime:\n      type: python\n      python:\n",
            "python must be a non-empty string",
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
