from __future__ import annotations

import argparse
from pathlib import Path

from atlas_operations.config_project import playbook_path, report_error, run_native, target_name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="config-apply")
    parser.add_argument("playbook")
    parser.add_argument("target")
    args = parser.parse_args(argv)
    root = Path.cwd()
    try:
        playbook = playbook_path(root, args.playbook)
        target = target_name(args.target)
        return run_native(
            "ansible-playbook",
            [str(playbook.relative_to(root)), "--limit", target],
            root,
        )
    except ValueError as error:
        return report_error(error)


if __name__ == "__main__":
    raise SystemExit(main())
