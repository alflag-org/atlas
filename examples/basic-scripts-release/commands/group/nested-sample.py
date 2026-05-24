from __future__ import annotations

import fire

from atlas_core import get_context


class NestedSample:
    """Meaningless nested sample command."""

    def show_context(self) -> dict[str, str]:
        ctx = get_context()
        return {
            "host": ctx.host.name,
            "site": ctx.host.site,
            "script": ctx.script.name,
            "release": ctx.script.release_name,
            "version": ctx.script.version,
            "release_root": str(ctx.script.release_root),
        }


def main() -> None:
    fire.Fire(NestedSample)


if __name__ == "__main__":
    main()
