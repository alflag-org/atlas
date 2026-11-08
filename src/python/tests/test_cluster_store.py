"""Credential exclusivity, strict state files and atomic durable replacement."""

import json
import os
from dataclasses import replace

import pytest

from atlas.cluster.config import load_config
from atlas.cluster.store import Credential, State, Store, private_file, read_json
from atlas_core.cluster import ClusterError

from .test_cluster_membership import member


def test_credentials_and_state(tmp_path):
    path = tmp_path / "credentials"
    credential = Credential.create(path)
    assert len(credential.key) == 32 and credential == Credential.read(path)
    assert credential.key.hex() not in repr(credential)
    with pytest.raises(FileExistsError):
        Credential.create(path)
    store = Store(tmp_path)
    assert store.read("a").membership == "uninitialized"
    state = State(
        "a",
        credential.cluster_id,
        "lab",
        "joined",
        {"a": member()},
        credential_fingerprint=credential.fingerprint,
    )
    with store.locked():
        with pytest.raises(ClusterError, match="already_running"), store.locked():
            pytest.fail("lock must reject a second daemon")
        store.write(state)
        assert store.read("a") == state
    with pytest.raises(ClusterError, match="invalid_state"):
        store.read("other")
    store.path.write_text('{"schema":"atlas.membership/v1"}')
    with pytest.raises(ClusterError, match="legacy_state_requires_reconfiguration"):
        store.read("a")


@pytest.mark.parametrize(
    "raw",
    [
        "[]",
        "no json",
        '{"cluster_id":1,"key":2}',
        '{"cluster_id":"bad","key":"00"}',
        "{}",
    ],
)
def test_reject_corrupt_credentials(tmp_path, raw):
    path = tmp_path / "credentials"
    path.write_text(raw)
    path.chmod(0o600)
    with pytest.raises(ClusterError):
        Credential.read(path)


def test_private_file_and_failed_replace(tmp_path, monkeypatch):
    path = tmp_path / "file"
    path.write_text("{}")
    path.chmod(0o644)
    with pytest.raises(ClusterError, match="unsafe_state"):
        private_file(path, os.O_RDONLY)
    path.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(OSError):
        read_json(link)
    store = Store(tmp_path)
    credential = Credential.create(tmp_path / "credentials")
    state = State(
        "a",
        credential.cluster_id,
        "lab",
        "joined",
        {"a": member()},
        credential_fingerprint=credential.fingerprint,
    )
    store.write(state)
    saved = store.path.read_bytes()

    def fail(*args):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        store.write(replace(state, name="changed"))
    assert store.path.read_bytes() == saved
    assert not list(tmp_path.glob("tmp*"))


def test_config_strict_yaml(tmp_path):
    path = tmp_path / "cluster.yml"
    path.write_text(
        json.dumps(
            {
                "bind": "0.0.0.0:7947",
                "advertise": "a.example.test",
                "credentials_file": str(tmp_path / "credentials"),
                "labels": {"zone": "lab"},
            }
        )
    )
    assert load_config(path).labels == {"zone": "lab"}
    for raw in (
        "[]",
        "{bind: a, bind: b}",
        "[",
        "{}",
        '{"registry":"old"}',
        '{"bind":"a","advertise":"b","credentials_file":"relative"}',
    ):
        path.write_text(raw)
        with pytest.raises(ClusterError):
            load_config(path)


@pytest.mark.parametrize(
    "changes",
    [
        {"extra": True},
        {"credential_fingerprint": "00"},
        {"membership": "unknown"},
        {"membership": "left"},
        {"seeds": "bad"},
        {"seeds": [None] * 33},
    ],
)
def test_invalid_state_is_never_reinitialized(tmp_path, changes):
    credential = Credential.create(tmp_path / "credential")
    state = State(
        "a",
        credential.cluster_id,
        "lab",
        "joined",
        {"a": member()},
        credential_fingerprint=credential.fingerprint,
    )
    store = Store(tmp_path)
    raw = json.dumps(state.document() | changes)
    store.path.write_text(raw)
    store.path.chmod(0o600)
    with pytest.raises(ClusterError, match="invalid_state"):
        store.read("a")
    assert store.path.read_text() == raw


def test_wrong_key_length_and_storage_size_limit(tmp_path):
    credential = Credential.create(tmp_path / "credential")
    path = tmp_path / "credential"
    path.write_text(json.dumps({"cluster_id": credential.cluster_id, "key": "00"}))
    with pytest.raises(ClusterError, match="invalid_credentials"):
        Credential.read(path)
    store = Store(tmp_path)
    with pytest.raises(ClusterError, match="membership_too_large"):
        store.write(State("a", name="x" * (1024 * 1024)))
    assert not store.path.exists()
