from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from atlas.cluster.node import Node
from atlas.cluster.store import Store
from atlas_core.cluster import ClusterError

from .test_cluster_membership import metadata


def test_concurrent_registry_writers_preserve_all_members(tmp_path):
    node = Node(tmp_path / "registry")
    node.initialize("lab", metadata("registry"))
    source = (
        "import json,sys; from pathlib import Path; from atlas.cluster.node import Node; "
        "Node(Path(sys.argv[1])).registry_request(json.loads(sys.argv[2]))"
    )
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                source,
                str(node.store.directory),
                json.dumps({"method": "join", "member": metadata(f"worker-{i}")}),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for i in range(4)
    ]
    for process in processes:
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        assert not stdout
    snapshot = node.registry_request({"method": "members"})
    assert len(snapshot["members"]) == 5 and snapshot["revision"] == 5
    saved = node.store.path.read_bytes()
    node.registry_request({"method": "members"})
    assert node.store.path.read_bytes() == saved


def test_atomic_write_failure_preserves_registry(tmp_path, monkeypatch):
    node = Node(tmp_path / "registry")
    node.initialize("lab", metadata("registry"))
    saved = node.store.path.read_bytes()

    def fail(*args):
        raise OSError("disk failure")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        node.registry_request({"method": "join", "member": metadata("worker")})
    assert node.store.path.read_bytes() == saved
    assert sorted(p.name for p in node.store.directory.iterdir()) == [
        ".lock",
        "membership.json",
    ]
    with pytest.raises(ClusterError, match="membership_too_large"):
        node.store.write({"huge": "x" * 1024 * 1024})
    assert node.store.path.read_bytes() == saved


@pytest.mark.parametrize("target", ["directory", "membership.json", ".lock"])
def test_shared_state_permissions_are_rejected(tmp_path, target):
    node = Node(tmp_path / "registry")
    node.initialize("lab", metadata("registry"))
    path = (
        node.store.directory if target == "directory" else node.store.directory / target
    )
    path.chmod(0o755 if target == "directory" else 0o644)
    with pytest.raises(ClusterError, match="unsafe_state"):
        node.registry_request({"method": "members"})


@pytest.mark.parametrize("target", ["directory", "membership.json", ".lock"])
def test_state_symlinks_are_not_followed(tmp_path, target):
    node = Node(tmp_path / "registry")
    node.initialize("lab", metadata("registry"))
    path = (
        node.store.directory if target == "directory" else node.store.directory / target
    )
    saved = tmp_path / "original"
    path.rename(saved)
    path.symlink_to(saved)
    with pytest.raises((ClusterError, OSError)):
        node.registry_request({"method": "members"})


@pytest.mark.parametrize(
    "data",
    [
        "{",
        "null",
        "{}",
        '{"schema":"old"}',
        "x" * (1024 * 1024 + 1),
        json.dumps({"schema": "atlas.membership/v1"}),
        json.dumps(
            {
                "schema": "atlas.membership/v1",
                "member": metadata("registry"),
                "registry": None,
                "snapshot": None,
            }
        ),
        json.dumps(
            {
                "schema": "atlas.membership/v1",
                "member": metadata("worker"),
                "registry": "bad;host",
                "snapshot": None,
            }
        ),
    ],
)
def test_corrupt_state_is_not_reset(tmp_path, data):
    node = Node(tmp_path / "registry")
    node.initialize("lab", metadata("registry"))
    node.store.path.write_text(data)
    with pytest.raises(ClusterError, match="invalid_state"):
        node.registry_request({"method": "members"})
    assert node.store.path.read_text() == data


def test_state_location_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert Store().directory == tmp_path / "atlas/cluster"
    monkeypatch.delenv("XDG_STATE_HOME")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert Store().directory == tmp_path / ".local/state/atlas/cluster"
