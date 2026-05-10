from __future__ import annotations

import fire

from atlas_core.context import get_context


class NestedSample:
    """Meaningless nested sample command."""

    def show_context(self) -> dict[str, str]:
        ctx = get_context()
        return {
            "host": ctx.host.name,
            "site": ctx.host.site,
            "script": ctx.script.name,
            "version": ctx.script.version,
        }


def main() -> None:
    fire.Fire(NestedSample)


if __name__ == "__main__":
    main()
