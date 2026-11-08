"""The sample service owns its models, errors and protocol version."""

from dataclasses import dataclass
from pathlib import Path

from atlas_core.ipc import Client, IPCError


@dataclass(frozen=True)
class SampleStatus:
    message: str


class SampleError(Exception):
    """The sample service could not complete a request."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class SampleClient:
    def __init__(self, socket: Path) -> None:
        self._client = Client(socket, api_version="sample.service/v1")

    async def status(self) -> SampleStatus:
        try:
            return SampleStatus(**await self._client.request("status"))
        except IPCError as exc:
            raise SampleError(exc.code) from None
