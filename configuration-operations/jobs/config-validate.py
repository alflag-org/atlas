from __future__ import annotations

import argparse
from pathlib import Path

from atlas_configuration_operations.config_project import (
    playbook_path,
    report_error,
    run_native,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="config-validate")
    parser.add_argument("playbook")
    args = parser.parse_args(argv)
    root = Path.cwd()
    try:
        playbook = playbook_path(root, args.playbook)
        return run_native(
            "ansible-playbook",
            [str(playbook.relative_to(root)), "--syntax-check"],
            root,
        )
    except ValueError as error:
        return report_error(error)


if __name__ == "__main__":
    raise SystemExit(main())
