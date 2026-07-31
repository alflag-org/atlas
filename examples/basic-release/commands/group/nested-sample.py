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
            "artifact": ctx.artifact.name,
            "release": ctx.artifact.release_name,
            "version": ctx.artifact.version,
            "release_root": str(ctx.artifact.release_root),
        }


def main() -> None:
    fire.Fire(NestedSample)


if __name__ == "__main__":
    main()
