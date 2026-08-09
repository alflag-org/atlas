from __future__ import annotations

import argparse
from pathlib import Path

from atlas_configuration_operations.config_project import report_error, run_native


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(prog="inventory-show").parse_args(argv)
    try:
        return run_native("ansible-inventory", ["--graph"], Path.cwd())
    except ValueError as error:
        return report_error(error)
