"""Single-writer durable Cluster state and transaction boundaries."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import stat
from pathlib import Path
from uuid import uuid4


class Store:
    def __init__(self, directory: Path) -> None:
        path = directory / "cluster.sqlite3"
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise ValueError("Cluster database must be an owner-only regular file")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise
        self.lock = fd
        try:
            self.db = sqlite3.connect(path)
            self.db.row_factory = sqlite3.Row
            self._initialize()
        except BaseException:
            os.close(self.lock)
            raise

    def _initialize(self) -> None:
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("Cluster database integrity check failed")
            self._schema()
        except BaseException:
            self.db.close()
            raise

    def _schema(self) -> None:
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS members (
                node_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, endpoint TEXT NOT NULL,
                certificate TEXT NOT NULL, state TEXT NOT NULL, permissions TEXT NOT NULL,
                labels TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS invitations (
                token_id TEXT PRIMARY KEY, secret_hash TEXT NOT NULL, expires REAL NOT NULL,
                consumed INTEGER NOT NULL DEFAULT 0, permissions TEXT NOT NULL, labels TEXT NOT NULL, node_id TEXT, csr TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY, timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                action TEXT NOT NULL, subject TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS operations (
                operation_id TEXT PRIMARY KEY, digest TEXT NOT NULL, plan TEXT NOT NULL,
                state TEXT NOT NULL, epoch INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS executions (
                operation_id TEXT NOT NULL, step_id TEXT NOT NULL, node_id TEXT NOT NULL,
                state TEXT NOT NULL, exit_code INTEGER, run_id TEXT,
                PRIMARY KEY (operation_id, step_id, node_id)
            );
        """)
        with self.db:
            if self.get("node_id") is None:
                self.set("node_id", str(uuid4()))
                self.set("revision", 0)
                self.set("epoch", 0)
            # A crash may have occurred after an external side effect, before commit.
            self.db.execute(
                "UPDATE executions SET state='unknown' WHERE state IN ('prepared','running')"
            )
            self.db.execute(
                "UPDATE operations SET state='unknown' WHERE state IN ('prepared','running')"
            )

    def get(self, key: str, default=None):
        row = self.db.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
        return default if row is None else json.loads(row[0])

    def set(self, key: str, value) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value))
        )

    def event(self, action: str, subject: str) -> None:
        self.db.execute(
            "INSERT INTO events(action,subject) VALUES (?,?)", (action, subject)
        )

    def members(self) -> list[dict]:
        return [
            dict(row)
            | {
                "permissions": json.loads(row["permissions"]),
                "labels": json.loads(row["labels"]),
            }
            for row in self.db.execute(
                "SELECT node_id,display_name,endpoint,state,permissions,labels FROM members ORDER BY node_id"
            )
        ]

    def add_member(self, member: dict, certificate: str) -> None:
        self.db.execute(
            "INSERT INTO members VALUES (?,?,?,?,?,?,?)",
            (
                member["node_id"],
                member["display_name"],
                member["endpoint"],
                certificate,
                "active",
                json.dumps(member["permissions"]),
                json.dumps(member["labels"]),
            ),
        )
        self.set("revision", self.get("revision") + 1)
        self.event("admitted", member["node_id"])

    def close(self) -> None:
        self.db.close()
        os.close(self.lock)
