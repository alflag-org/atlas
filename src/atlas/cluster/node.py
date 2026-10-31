"""Explicit node admission and live authority authorization."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from uuid import UUID, uuid4

from atlas_core.ipc import IPCError, Peer, Unavailable

from . import identity, network
from .store import Store

ADMIN_METHODS = {"invite", "remove"}
READ_METHODS = {"members", "authorize", "operation.inspect", "execution.inspect"}


def identifier(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise IPCError("invalid_id") from None


def text(value: object, *, limit: int = 128, multiline: bool = False) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > limit
        or any(ord(c) < 32 and not (multiline and c == "\n") for c in value)
    ):
        raise IPCError("invalid_request")
    return value


class Node:
    def __init__(self, directory: Path, address: str | None, display_name: str) -> None:
        if address is not None:
            identity.endpoint(address)
        self.directory = directory
        self.address = address
        self.display_name = text(display_name)
        self.store = Store(directory)
        self.node_id = self.store.get("node_id")
        self.listener = network.Listener(self.remote)
        from .workflow import WorkflowEngine

        self.workflows = WorkflowEngine(self)

    @property
    def authority(self) -> bool:
        return self.store.get("authority_id") == self.node_id

    def status(self) -> dict:
        return {
            "cluster_id": self.store.get("cluster_id"),
            "name": self.store.get("name"),
            "node_id": self.node_id,
            "authority_id": self.store.get("authority_id"),
            "revision": self.store.get("revision"),
            "stale": not self.authority and self.store.get("cluster_id") is not None,
        }

    async def start(self) -> None:
        if self.store.get("cluster_id") is not None and self.listener.server is None:
            await self.listener.start(self.store.get("endpoint"), self.tls(server=True))

    def tls(self, *, server: bool = False):
        return identity.tls_context(
            self.directory,
            self.store.get("ca"),
            key=self.store.get("key"),
            certificate=self.store.get("certificate"),
            server=server,
        )

    async def close(self) -> None:
        await self.listener.close()
        await self.workflows.close()
        self.store.close()

    def _unjoined(self) -> None:
        if self.store.get("cluster_id") is not None:
            raise IPCError("already_joined")
        if self.address is None:
            raise IPCError("endpoint_required")

    async def initialize(self, params: dict) -> dict:
        self._unjoined()
        if self.store.get("join_pending"):
            raise IPCError("join_outcome_unknown")
        name = text(params.get("name"))
        cluster_id, ca_key, key = str(uuid4()), identity.new_key(), identity.new_key()
        ca = identity.issue(ca_key, cluster_id)
        cert = identity.issue(
            ca_key,
            cluster_id,
            csr=identity.request_identity(key, self.node_id),
            node_id=self.node_id,
        )
        with self.store.db:
            for field, value in {
                "cluster_id": cluster_id,
                "name": name,
                "authority_id": self.node_id,
                "authority_endpoint": self.address,
                "endpoint": self.address,
                "key": key,
                "ca_key": ca_key,
                "certificate": cert,
                "ca": ca,
            }.items():
                self.store.set(field, value)
            self.store.add_member(
                {
                    "node_id": self.node_id,
                    "display_name": self.display_name,
                    "endpoint": self.address,
                    "permissions": ["read", "execute", "admin"],
                    "labels": {},
                },
                cert,
            )
        await self.start()
        return self.status()

    def invitation(self, params: dict) -> dict:
        ttl = params.get("ttl_seconds", 600)
        permissions = params.get("permissions", ["read", "execute"])
        labels = params.get("labels", {})
        if type(ttl) is not int or not 1 <= ttl <= 3600:
            raise IPCError("invalid_expiry")
        if (
            not isinstance(permissions, list)
            or not permissions
            or any(p not in {"read", "execute", "admin"} for p in permissions)
        ):
            raise IPCError("invalid_permissions")
        if not isinstance(labels, dict):
            raise IPCError("invalid_labels")
        labels = {text(k): text(v) for k, v in labels.items()}
        token_id, secret, expires = (
            str(uuid4()),
            secrets.token_urlsafe(32),
            time.time() + ttl,
        )
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO invitations(token_id,secret_hash,expires,permissions,labels) VALUES (?,?,?,?,?)",
                (
                    token_id,
                    hashlib.sha256(secret.encode()).hexdigest(),
                    expires,
                    json.dumps(permissions),
                    json.dumps(labels),
                ),
            )
            self.store.event("invited", token_id)
        return {
            "schema": "atlas.cluster-invite/v1",
            "cluster_id": self.store.get("cluster_id"),
            "authority_id": self.node_id,
            "authority_endpoint": self.store.get("endpoint"),
            "ca": self.store.get("ca"),
            "token_id": token_id,
            "secret": secret,
            "expires": expires,
        }

    def admit(self, params: dict) -> dict:
        if not self.authority:
            raise IPCError("not_authority")
        invite = params.get("invite", {})
        token_id = identifier(invite.get("token_id"))
        secret = text(invite.get("secret"))
        node_id = identifier(params.get("node_id"))
        address = text(params.get("endpoint"))
        identity.endpoint(address)
        display_name = text(params.get("display_name"))
        csr = text(params.get("csr"), limit=4096, multiline=True)
        with self.store.db:
            token = self.store.db.execute(
                "SELECT * FROM invitations WHERE token_id=?", (token_id,)
            ).fetchone()
            if (
                token is None
                or token["consumed"]
                or token["expires"] <= time.time()
                or invite.get("cluster_id") != self.store.get("cluster_id")
                or not hmac.compare_digest(
                    token["secret_hash"], hashlib.sha256(secret.encode()).hexdigest()
                )
            ):
                raise IPCError("invalid_invitation")
            if self.store.db.execute(
                "SELECT 1 FROM members WHERE node_id=?", (node_id,)
            ).fetchone():
                raise IPCError("identity_exists")
            certificate = identity.issue(
                self.store.get("ca_key"),
                self.store.get("cluster_id"),
                csr=csr,
                node_id=node_id,
            )
            member = {
                "node_id": node_id,
                "display_name": display_name,
                "endpoint": address,
                "permissions": json.loads(token["permissions"]),
                "labels": json.loads(token["labels"]),
            }
            self.store.add_member(member, certificate)
            self.store.db.execute(
                "UPDATE invitations SET consumed=1,node_id=?,csr=? WHERE token_id=?",
                (node_id, csr, token_id),
            )
        return {
            "certificate": certificate,
            "name": self.store.get("name"),
            "revision": self.store.get("revision"),
        }

    def admission(self, params: dict) -> dict:
        """Recover a committed admission without consuming or creating an invite."""
        if not self.authority:
            raise IPCError("not_authority")
        invite = params.get("invite", {})
        token = self.store.db.execute(
            "SELECT * FROM invitations WHERE token_id=?",
            (identifier(invite.get("token_id")),),
        ).fetchone()
        if (
            token is None
            or not token["consumed"]
            or token["node_id"] != params.get("node_id")
            or token["csr"] != params.get("csr")
            or not hmac.compare_digest(
                token["secret_hash"],
                hashlib.sha256(text(invite.get("secret")).encode()).hexdigest(),
            )
        ):
            raise IPCError("admission_not_found")
        member = self.authorize(token["node_id"], "read")
        return {
            "certificate": member["certificate"],
            "name": self.store.get("name"),
            "revision": self.store.get("revision"),
        }

    async def join(self, params: dict) -> dict:
        self._unjoined()
        invite = params.get("invite", {})
        pending = self.store.get("join_pending")
        resume = params.get("reconcile", False)
        if pending and not resume:
            raise IPCError("join_outcome_unknown")
        if invite.get("schema") != "atlas.cluster-invite/v1" or (
            not resume and invite.get("expires", 0) <= time.time()
        ):
            raise IPCError("invalid_invitation")
        authority_id = identifier(invite.get("authority_id"))
        cluster_id = identifier(invite.get("cluster_id"))
        address = text(invite.get("authority_endpoint"))
        identity.endpoint(address)
        ca = text(invite.get("ca"), limit=8192, multiline=True)
        invite_digest = hashlib.sha256(
            json.dumps(invite, sort_keys=True).encode()
        ).hexdigest()
        if resume:
            if pending != invite_digest:
                raise IPCError("admission_not_found")
            key, csr = self.store.get("key"), self.store.get("csr")
        else:
            key = identity.new_key()
            csr = identity.request_identity(key, self.node_id)
        context = identity.tls_context(self.directory, ca)
        with self.store.db:
            self.store.set("join_pending", invite_digest)
            self.store.set("key", key)
            self.store.set("csr", csr)
        try:
            result = await network.call(
                address,
                authority_id,
                context,
                "admission" if resume else "join",
                {
                    "invite": invite,
                    "node_id": self.node_id,
                    "endpoint": self.address,
                    "display_name": self.display_name,
                    "csr": csr,
                },
            )
        except Unavailable:
            raise
        except IPCError as exc:
            if exc.code in {
                "invalid_invitation",
                "identity_exists",
                "invalid_identity",
            }:
                with self.store.db:
                    self.store.set("join_pending", None)
            raise
        with self.store.db:
            for field, value in {
                "cluster_id": cluster_id,
                "name": result["name"],
                "authority_id": authority_id,
                "authority_endpoint": address,
                "endpoint": self.address,
                "certificate": result["certificate"],
                "ca": ca,
                "revision": result["revision"],
                "join_pending": None,
            }.items():
                self.store.set(field, value)
        await self.start()
        return self.status()

    def authorize(
        self, node_id: str, permission: str, certificate: str | None = None
    ) -> dict:
        member = self.store.db.execute(
            "SELECT * FROM members WHERE node_id=?", (node_id,)
        ).fetchone()
        if (
            member is None
            or member["state"] != "active"
            or permission not in json.loads(member["permissions"])
            or (
                certificate is not None
                and not hmac.compare_digest(member["certificate"], certificate)
            )
        ):
            raise IPCError("forbidden")
        return dict(member)

    async def authority_call(self, method: str, params: dict) -> dict:
        if self.store.get("cluster_id") is None:
            raise IPCError("not_joined")
        if self.authority:
            return await self.remote(
                method, params, (self.node_id, self.store.get("certificate"))
            )
        return await network.call(
            self.store.get("authority_endpoint"),
            self.store.get("authority_id"),
            self.tls(),
            method,
            params,
            timeout=300,
        )

    async def local(self, method: str, params: dict, _peer: Peer) -> dict:
        if method == "status":
            return self.status()
        if method == "init":
            return await self.initialize(params)
        if method == "join":
            return await self.join(params)
        if method == "leave":
            return await self.authority_call("leave", {})
        if method in {
            "members",
            "invite",
            "remove",
            "plan",
            "apply",
            "operation.inspect",
            "operation.reconcile",
        }:
            return await self.authority_call(method, params)
        raise IPCError("unknown_method")

    async def remote(
        self, method: str, params: dict, peer: tuple[str, str] | None
    ) -> dict:
        if method == "join":
            return self.admit(params)
        if method == "admission":
            return self.admission(params)
        if peer is None:
            raise IPCError("forbidden")
        node_id, certificate = peer
        permission = (
            "admin"
            if method in ADMIN_METHODS
            else "read"
            if method in READ_METHODS
            else "execute"
        )
        if self.authority:
            self.authorize(node_id, permission, certificate)
        else:
            # A valid CA signature alone cannot authorize a revoked peer.
            await self.authority_call(
                "authorize",
                {
                    "node_id": node_id,
                    "permission": permission,
                    "certificate": certificate,
                },
            )
        if method == "authorize" and self.authority:
            self.authorize(
                identifier(params.get("node_id")),
                text(params.get("permission")),
                text(params.get("certificate"), limit=8192, multiline=True),
            )
            return {"authorized": True, "revision": self.store.get("revision")}
        if method == "members" and self.authority:
            return {
                "members": self.store.members(),
                "revision": self.store.get("revision"),
                "stale": False,
            }
        if method == "invite" and self.authority:
            return self.invitation(params)
        if method in {"remove", "leave"} and self.authority:
            target = node_id if method == "leave" else identifier(params.get("node_id"))
            if target == self.node_id:
                raise IPCError("cannot_remove_authority")
            with self.store.db:
                if not self.store.db.execute(
                    "UPDATE members SET state='removed' WHERE node_id=? AND state='active'",
                    (target,),
                ).rowcount:
                    raise IPCError("unknown_member")
                self.store.set("revision", self.store.get("revision") + 1)
                self.store.event("removed", target)
            return {"removed": target}
        return await self.workflows.request(method, params, node_id)
