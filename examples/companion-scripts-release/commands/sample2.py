from __future__ import annotations

import fire

from atlas_core.context import get_context


class Sample2:
    def show_release(self) -> dict[str, str]:
        ctx = get_context()
        return {
            "script": ctx.script.name,
            "version": ctx.script.version,
            "host": ctx.host.name,
        }


def main() -> None:
    fire.Fire(Sample2)


if __name__ == "__main__":
    main()
