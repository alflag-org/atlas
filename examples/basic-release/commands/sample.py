from __future__ import annotations

import fire

from atlas_core.context import get_context
from samplelib.message import build_message


class Sample:
    """Meaningless sample command."""

    def hello(self, name: str = "world") -> None:
        ctx = get_context()
        print(build_message(name=name, host=ctx.host.name, artifact=ctx.artifact.name))

    def echo(self, value: str) -> str:
        return value


def main() -> None:
    fire.Fire(Sample)


if __name__ == "__main__":
    main()
