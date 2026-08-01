from __future__ import annotations

import argparse
from pathlib import Path

from atlas_configuration_operations.config_project import (
    inventory_path,
    report_error,
    run_native,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="inventory-refresh")
    parser.add_argument("--site", required=True)
    args = parser.parse_args(argv)
    root = Path.cwd()
    try:
        inventory = inventory_path(root, args.site)
        return run_native(
            "ansible-inventory",
            [
                "-i",
                str(inventory.relative_to(root)),
                "--graph",
                "--flush-cache",
            ],
            root,
        )
    except ValueError as error:
        return report_error(error)


if __name__ == "__main__":
    raise SystemExit(main())
