from __future__ import annotations

import sqlite3
from unittest.mock import Mock

import pytest

from atlas.cluster.store import Store


def test_state_is_exclusive_private_and_preserves_uncertain_execution(tmp_path):
    store = Store(tmp_path)
    node_id = store.get("node_id")
    with pytest.raises(BlockingIOError):
        Store(tmp_path)
    with store.db:
        store.db.execute(
            "INSERT INTO executions VALUES ('op','step','node','running',NULL,NULL)"
        )
        store.db.execute(
            "INSERT INTO operations VALUES ('op','digest','{}','running',1)"
        )
        store.set("lease", {"operation_id": "op", "epoch": 1})
    store.close()
    restored = Store(tmp_path)
    try:
        assert restored.get("node_id") == node_id
        assert (
            restored.db.execute("SELECT state FROM executions").fetchone()[0]
            == "unknown"
        )
        assert (
            restored.db.execute("SELECT state FROM operations").fetchone()[0]
            == "unknown"
        )
        assert restored.get("lease")["operation_id"] == "op"
    finally:
        restored.close()
    path = tmp_path / "cluster.sqlite3"
    path.chmod(0o644)
    with pytest.raises(ValueError, match="owner-only"):
        Store(tmp_path)
    path.chmod(0o600)
    path.write_bytes(b"corrupted database")
    with pytest.raises(sqlite3.DatabaseError):
        Store(tmp_path)


def test_integrity_failure_closes_connection_and_releases_lock(tmp_path, monkeypatch):
    connection = Mock()
    connection.execute.return_value.fetchone.return_value = ["corrupt page"]
    monkeypatch.setattr(sqlite3, "connect", lambda path: connection)
    with pytest.raises(ValueError, match="integrity"):
        Store(tmp_path)
    connection.close.assert_called_once()
    connection.reset_mock()
    with pytest.raises(ValueError, match="integrity"):
        Store(tmp_path)
    connection.close.assert_called_once()
