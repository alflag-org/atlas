from __future__ import annotations

from pathlib import Path

import pytest

from atlas.config import load_config


def _write_config(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_load_config_reads_runtime_scripts_and_registries(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "config.yml",
        """runtime:
  python:
    version: "3.12.3"
scripts:
  source: sample-release
  auto_update: true
  registries:
    sample-release:
      source: "git+https://example.test/scripts.git#v1.0.0"
    local-release: "/opt/releases/local"
""",
    )

    config = load_config(path)

    assert config.path == path
    assert config.runtime.python_version == "3.12.3"
    assert config.scripts.source == "sample-release"
    assert list(config.scripts.releases) == ["default"]
    assert config.scripts.releases["default"].source == "sample-release"
    assert config.scripts.auto_update is True
    assert config.scripts.registries["sample-release"].source == "git+https://example.test/scripts.git#v1.0.0"
    assert config.scripts.registries["local-release"].source == "/opt/releases/local"


def test_load_config_reads_multiple_releases(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "config.yml",
        """runtime:
  python:
    version: "3.12.3"
scripts:
  releases:
    common: common
    kitsunebi:
      source: kitsunebi
      enabled: false
  registries:
    common:
      source: "git+https://example.test/common.git#v0.1.0"
    kitsunebi:
      source: "git+https://example.test/kitsunebi.git#v0.1.0"
""",
    )

    config = load_config(path)

    assert config.scripts.source is None
    assert list(config.scripts.releases) == ["common", "kitsunebi"]
    assert config.scripts.releases["common"].source == "common"
    assert config.scripts.releases["common"].enabled is True
    assert config.scripts.releases["kitsunebi"].source == "kitsunebi"
    assert config.scripts.releases["kitsunebi"].enabled is False


def test_load_config_treats_null_registries_as_empty(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "config.yml",
        """runtime:
  python:
    version: "3.12"
scripts:
  source: "/opt/releases/current"
  registries:
""",
    )

    config = load_config(path)

    assert config.scripts.registries == {}


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("[]\n", "config.yml must be a mapping"),
        (
            """scripts:
  source: "/opt/releases/current"
""",
            "runtime section is required",
        ),
        (
            """runtime: {}
scripts:
  source: "/opt/releases/current"
""",
            "runtime.python section is required",
        ),
        (
            """runtime:
  python:
    version: ""
scripts:
  source: "/opt/releases/current"
""",
            "runtime.python.version is required",
        ),
        (
            """runtime:
  python:
    version: "3.12"
""",
            "scripts section is required",
        ),
        (
            """runtime:
  python:
    version: "3.12"
scripts:
  source: ""
""",
            "scripts.source is required",
        ),
        (
            """runtime:
  python:
    version: "3.12"
scripts:
  source: "/opt/releases/current"
  releases: []
""",
            "scripts.releases must be a mapping",
        ),
        (
            """runtime:
  python:
    version: "3.12"
scripts:
  releases:
    current: sample
""",
            "invalid release name: current",
        ),
        (
            """runtime:
  python:
    version: "3.12"
scripts:
  releases:
    common: []
""",
            "scripts.releases.common must be a mapping or string",
        ),
        (
            """runtime:
  python:
    version: "3.12"
scripts:
  releases:
    common:
      source: ""
""",
            "scripts.releases.common.source is required",
        ),
        (
            """runtime:
  python:
    version: "3.12"
scripts:
  source: "/opt/releases/current"
  registries: []
""",
            "scripts.registries must be a mapping",
        ),
        (
            """runtime:
  python:
    version: "3.12"
scripts:
  source: "/opt/releases/current"
  registries:
    "": "/opt/releases/current"
""",
            "scripts.registries alias must not be empty",
        ),
        (
            """runtime:
  python:
    version: "3.12"
scripts:
  source: "/opt/releases/current"
  registries:
    broken: []
""",
            "scripts.registries.broken must be a mapping or string",
        ),
        (
            """runtime:
  python:
    version: "3.12"
scripts:
  source: "/opt/releases/current"
  registries:
    broken:
      source: ""
""",
            "scripts.registries.broken.source is required",
        ),
    ],
)
def test_load_config_rejects_invalid_config(tmp_path: Path, body: str, message: str) -> None:
    path = _write_config(tmp_path / "config.yml", body)

    with pytest.raises(ValueError, match=message):
        load_config(path)
