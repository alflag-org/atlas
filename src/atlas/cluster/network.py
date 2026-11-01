"""One bounded membership request over the operator's existing SSH setup."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import signal

from atlas_core.cluster import ClusterError, ClusterUnavailable

from .store import MAX_BYTES


def destination(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?[A-Za-z0-9][A-Za-z0-9_.:-]*", value
    ):
        raise ClusterError("invalid_address")
    return value


async def request(address: str, payload: dict, *, timeout: float) -> dict:
    destination(address)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    data = json.dumps(payload).encode()
    if len(data) > MAX_BYTES:
        raise ClusterError("membership_too_large")
    try:
        async with asyncio.timeout(timeout):
            process = await asyncio.create_subprocess_exec(
                "ssh",
                "-T",
                "-oBatchMode=yes",
                "-oStrictHostKeyChecking=yes",
                "-oConnectTimeout=5",
                "--",
                address,
                "atlas cluster _registry",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                process.stdin.write(data)
                await process.stdin.drain()
                process.stdin.close()
                try:
                    response = await process.stdout.readexactly(MAX_BYTES + 1)
                except asyncio.IncompleteReadError as exc:
                    response = exc.partial
                if len(response) > MAX_BYTES:
                    raise ClusterError("invalid_response")
                returncode = await process.wait()
                if returncode != 0:
                    raise ClusterUnavailable("registry_unavailable")
            finally:
                # A ProxyCommand can retain stdout after the SSH parent has exited.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
    except (OSError, TimeoutError):
        raise ClusterUnavailable("registry_unavailable") from None
    try:
        result = json.loads(response)
        if result["schema"] != "atlas.membership-rpc/v1":
            raise ValueError
        if "error" in result:
            code = result["error"]
            if not isinstance(code, str) or not re.fullmatch(r"[a-z_]{1,64}", code):
                raise ValueError
            raise ClusterError(code)
        return result["result"]
    except (ValueError, KeyError, TypeError):
        raise ClusterError("invalid_response") from None
