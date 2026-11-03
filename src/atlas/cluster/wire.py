"""Authenticated envelopes. Cryptography is imported only by the daemon."""

import base64
import binascii
import json
import os
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from atlas_core.cluster import ClusterError

from .model import encoded
from .store import Credential

MAX_FRAME = 1024 * 1024
MAX_DATAGRAM = 1200
VERSION = 1
KINDS = {"challenge", "request", "response", "ping", "ack", "ping_req", "gossip"}


class Cipher:
    def __init__(self, credential: Credential) -> None:
        self.cluster_id = credential.cluster_id
        self.fingerprint = credential.fingerprint
        self._cipher = AESGCM(credential.key)

    def seal(self, kind: str, payload: dict, *, limit: int = MAX_FRAME) -> bytes:
        # Independent 256-bit OS-random nonces avoid shared counters across senders
        # and restarts. Collision probability is negligible, not mathematically zero.
        nonce = os.urandom(32)
        header = [VERSION, self.cluster_id, kind]
        ciphertext = self._cipher.encrypt(nonce, encoded(payload), encoded(header))
        packet = encoded(
            {
                "header": header,
                "nonce": nonce.hex(),
                "data": base64.b64encode(ciphertext).decode("ascii"),
            }
        )
        if len(packet) > limit:
            raise ClusterError("message_too_large")
        return packet

    def open(self, packet: bytes, *, limit: int = MAX_FRAME) -> tuple[str, dict]:
        try:
            if len(packet) > limit:
                raise ValueError
            envelope = json.loads(packet)
            header = envelope["header"]
            if (
                not isinstance(header, list)
                or len(header) != 3
                or type(header[0]) is not int
                or header[0] != VERSION
                or header[1] != self.cluster_id
                or header[2] not in KINDS
            ):
                raise ValueError
            nonce = bytes.fromhex(envelope["nonce"])
            if len(nonce) != 32:
                raise ValueError
            ciphertext = base64.b64decode(envelope["data"], validate=True)
            payload = json.loads(
                self._cipher.decrypt(nonce, ciphertext, encoded(header))
            )
            if not isinstance(payload, dict):
                raise TypeError
            return header[2], payload
        except (
            ValueError,
            TypeError,
            KeyError,
            InvalidTag,
            binascii.Error,
            RecursionError,
        ):
            raise ClusterError("authentication_or_format_failed") from None


async def read_frame(reader) -> bytes:
    length = struct.unpack("!I", await reader.readexactly(4))[0]
    if not 1 <= length <= MAX_FRAME:
        raise ClusterError("message_too_large")
    return await reader.readexactly(length)


async def write_frame(writer, packet: bytes) -> None:
    if len(packet) > MAX_FRAME:
        raise ClusterError("message_too_large")
    writer.write(struct.pack("!I", len(packet)) + packet)
    await writer.drain()
