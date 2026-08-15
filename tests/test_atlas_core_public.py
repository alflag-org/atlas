from pathlib import Path

from atlas_core import (
    AtlasContext,
    AtlasPaths,
    CommandInfo,
    ExecutionInfo,
    HostProfile,
    ProgramInfo,
    get_context,
    get_host,
    get_paths,
)


def test_public_api_exports_context_models() -> None:
    assert AtlasContext is not None
    assert AtlasPaths is not None
    assert CommandInfo is not None
    assert ExecutionInfo is not None
    assert HostProfile is not None
    assert ProgramInfo is not None
    assert get_context is not None
    assert get_host is not None
    assert get_paths is not None
    assert ProgramInfo("tool", Path("/tool"), "python", "3.13", Path("/venv")).to_dict()["runtime"] == {
        "type": "python",
        "python": "3.13",
        "venv": "/venv",
    }
