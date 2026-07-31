from __future__ import annotations

from pathlib import Path

import pytest

from atlas.config import load_config


def _write_config(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_load_config_reads_runtime_and_releases(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "config.yml",
        """runtime:
  python:
    version: "3.12.3"
releases:
  common:
    source: "git+https://example.test/common.git#v0.1.0"
  operations:
    source: "/opt/releases/operations"
    enabled: false
""",
    )

    config = load_config(path)

    assert config.path == path
    assert config.runtime.python_version == "3.12.3"
    assert list(config.releases) == ["common", "operations"]
    assert config.releases["common"].source == "git+https://example.test/common.git#v0.1.0"
    assert config.releases["common"].enabled is True
    assert config.releases["operations"].source == "/opt/releases/operations"
    assert config.releases["operations"].enabled is False


@pytest.mark.parametrize(
    ("body", "expected_exception", "message"),
    [
        ("[]\n", TypeError, "config.yml must be a mapping"),
        (
            """releases: {}
""",
            TypeError,
            "runtime must be a mapping",
        ),
        (
            """runtime: []
releases: {}
""",
            TypeError,
            "runtime must be a mapping",
        ),
        (
            """runtime:
  python: []
releases: {}
""",
            TypeError,
            "runtime.python must be a mapping",
        ),
        (
            """runtime:
  python:
    version: ""
releases: {}
""",
            ValueError,
            "runtime.python.version is required",
        ),
        (
            """runtime:
  python:
    version: 312
releases: {}
""",
            ValueError,
            "runtime.python.version is required",
        ),
        (
            """runtime:
  python:
    version: "3.12"
releases: []
""",
            TypeError,
            "releases must be a mapping",
        ),
        (
            """runtime:
  python:
    version: "3.12"
releases:
  bad_name:
    source: sample
""",
            ValueError,
            "invalid release name: bad_name",
        ),
        (
            """runtime:
  python:
    version: "3.12"
releases:
  common: sample
""",
            TypeError,
            "releases.common must be a mapping",
        ),
        (
            """runtime:
  python:
    version: "3.12"
releases:
  common:
    source: ""
""",
            ValueError,
            "releases.common.source is required",
        ),
        (
            """runtime:
  python:
    version: "3.12"
releases:
  common:
    source: sample
    enabled: "yes"
""",
            TypeError,
            "releases.common.enabled must be a boolean",
        ),
        (
            """runtime:
  python:
    version: "3.12"
releases:
  common:
    source: sample
    extra: true
""",
            ValueError,
            "releases.common has unknown key: extra",
        ),
        (
            """runtime:
  python:
    version: "3.12"
releases: {}
scripts: {}
""",
            ValueError,
            "config.yml has unknown key: scripts",
        ),
        (
            """runtime:
  python:
    version: "3.12"
    extra: true
releases: {}
""",
            ValueError,
            "runtime.python has unknown key: extra",
        ),
        (
            """runtime:
  python:
    version: "3.12"
  extra: true
releases: {}
""",
            ValueError,
            "runtime has unknown key: extra",
        ),
    ],
)
def test_load_config_rejects_invalid_config(
    tmp_path: Path,
    body: str,
    expected_exception: type[Exception],
    message: str,
) -> None:
    path = _write_config(tmp_path / "config.yml", body)

    with pytest.raises(expected_exception, match=message):
        load_config(path)


def test_load_config_rejects_non_string_release_name(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "config.yml",
        """runtime:
  python:
    version: "3.12"
releases:
  1:
    source: sample
""",
    )

    with pytest.raises(TypeError, match="release name must be a string"):
        load_config(path)
