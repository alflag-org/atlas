from __future__ import annotations

import argparse
import subprocess
import sys


def _targets(argv_targets: list[str]) -> list[str]:
    candidates = list(argv_targets)
    if not sys.stdin.isatty():
        candidates.extend(sys.stdin.read().splitlines())
    seen: set[str] = set()
    result: list[str] = []
    for candidate in candidates:
        target = candidate.strip()
        if target and target not in seen:
            seen.add(target)
            result.append(target)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="config-diff-many")
    parser.add_argument("playbook")
    parser.add_argument("targets", nargs="*")
    args = parser.parse_args(argv)
    targets = _targets(args.targets)
    if not targets:
        print("at least one target is required", file=sys.stderr)
        return 2

    first_failure = 0
    for target in targets:
        print(f"==> {target} <==", file=sys.stderr)
        try:
            process = subprocess.run(
                ["config-diff", args.playbook, target],
                check=False,
                shell=False,
            )
            return_code = process.returncode
        except FileNotFoundError:
            print("config-diff command not found", file=sys.stderr)
            return_code = 127
        if first_failure == 0 and return_code != 0:
            first_failure = return_code
    return first_failure


if __name__ == "__main__":
    raise SystemExit(main())
