from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sys

from .config import load_config
from .paths import ensure_dirs, get_paths
from .runner import resolve_command_path, run_command
from .runtime import current_python_version, install_runtime, runtime_status
from .scripts import discover_commands, install_release, read_version, resolve_source
from .shims import ensure_script_runner, regenerate_shims


def _sync_atlas_core(home: Path) -> None:
    src = Path(__file__).resolve().parents[1] / "atlas_core"
    dst = home / "lib/python/atlas_core"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def _ensure_atlas_launcher(path: Path) -> None:
    content = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"exec {sys.executable} -m atlas.cli \"$@\"\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def cmd_status(_: argparse.Namespace) -> int:
    p = get_paths()
    ensure_dirs(p)
    config_path = p.etc / "config.yml"
    host_path = p.etc / "host.yml"
    version = ""
    count = 0
    if p.scripts.exists():
        version = read_version(p.scripts)
        count = len(discover_commands(p.scripts / "commands"))
    host_name = "unknown"
    if host_path.exists():
        import yaml

        raw = yaml.safe_load(host_path.read_text(encoding="utf-8")) or {}
        if isinstance(raw, dict):
            host_name = str(raw.get("name", "unknown"))
    print(f"config file path: {config_path}")
    print(f"host file path: {host_path}")
    print(f"host name: {host_name}")
    print(f"scripts current path: {p.scripts}")
    print(f"scripts version: {version}")
    print(f"commands count: {count}")
    print(f"python core path: {p.core_python}")
    print(f"python scripts path: {p.scripts_python}")
    print(f"shims path: {p.shims}")
    return 0


def cmd_runtime_status(_: argparse.Namespace) -> int:
    p = get_paths()
    st = runtime_status(p.runtime)
    print("python:")
    print(f"  core: {st['core']}")
    print(f"  scripts: {st['scripts']}")
    return 0


def cmd_runtime_install(_: argparse.Namespace) -> int:
    p = get_paths()
    ensure_dirs(p)
    configured = None
    config_path = p.etc / "config.yml"
    if config_path.exists():
        configured = load_config(config_path).runtime.python_version
    core, scripts = install_runtime(p.runtime)
    print(f"installed core python: {core}")
    print(f"installed scripts python: {scripts}")
    if configured is not None:
        actual = current_python_version()
        print(f"configured python version: {configured}")
        print(f"actual python version: {actual}")
        if not actual.startswith(f"{configured}.") and not actual.startswith(configured):
            print("warning: configured runtime.python.version does not match current interpreter")
    return 0


def _scripts_paths():
    p = get_paths()
    releases_root = p.home / "scripts" / "releases"
    current_link = p.home / "scripts" / "current"
    return p, releases_root, current_link


def cmd_scripts_install(args: argparse.Namespace) -> int:
    p, releases_root, current_link = _scripts_paths()
    ensure_dirs(p)
    source = resolve_source(args.source)
    install_release(source, releases_root, current_link)
    _sync_atlas_core(p.home)
    _ensure_atlas_launcher(p.bin_dir / "atlas")
    ensure_script_runner(p.script_runner, p.bin_dir / "atlas")
    names = regenerate_shims(current_link / "commands", p.shims, p.script_runner)
    print(f"installed scripts: {current_link}")
    print(f"commands: {len(names)}")
    return 0


def cmd_scripts_update(_: argparse.Namespace) -> int:
    p, releases_root, current_link = _scripts_paths()
    cfg = load_config(p.etc / "config.yml")
    source = resolve_source(cfg.scripts.source)
    install_release(source, releases_root, current_link)
    _sync_atlas_core(p.home)
    _ensure_atlas_launcher(p.bin_dir / "atlas")
    ensure_script_runner(p.script_runner, p.bin_dir / "atlas")
    regenerate_shims(current_link / "commands", p.shims, p.script_runner)
    return 0


def cmd_scripts_list(_: argparse.Namespace) -> int:
    p = get_paths()
    for entry in discover_commands(p.scripts / "commands"):
        print(entry.name)
    return 0


def cmd_scripts_shims(_: argparse.Namespace) -> int:
    p = get_paths()
    ensure_script_runner(p.script_runner, p.bin_dir / "atlas")
    names = regenerate_shims(p.scripts / "commands", p.shims, p.script_runner)
    print(f"generated shims: {len(names)}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    p = get_paths()
    ensure_dirs(p)
    return run_command(p, args.command_name, args.args)


def cmd_which(args: argparse.Namespace) -> int:
    p = get_paths()
    print(resolve_command_path(p.scripts, args.command_name))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="atlas")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status")
    p_status.set_defaults(func=cmd_status)

    p_runtime = sub.add_parser("runtime")
    runtime_sub = p_runtime.add_subparsers(dest="runtime_cmd", required=True)
    p_runtime_status = runtime_sub.add_parser("status")
    p_runtime_status.set_defaults(func=cmd_runtime_status)
    p_runtime_install = runtime_sub.add_parser("install")
    p_runtime_install.set_defaults(func=cmd_runtime_install)

    p_scripts = sub.add_parser("scripts")
    scripts_sub = p_scripts.add_subparsers(dest="scripts_cmd", required=True)
    p_scripts_install = scripts_sub.add_parser("install")
    p_scripts_install.add_argument("source")
    p_scripts_install.set_defaults(func=cmd_scripts_install)
    p_scripts_update = scripts_sub.add_parser("update")
    p_scripts_update.set_defaults(func=cmd_scripts_update)
    p_scripts_list = scripts_sub.add_parser("list")
    p_scripts_list.set_defaults(func=cmd_scripts_list)
    p_scripts_shims = scripts_sub.add_parser("shims")
    p_scripts_shims.set_defaults(func=cmd_scripts_shims)

    p_run = sub.add_parser("run")
    p_run.add_argument("command_name")
    p_run.add_argument("args", nargs=argparse.REMAINDER)
    p_run.set_defaults(func=cmd_run)

    p_which = sub.add_parser("which")
    p_which.add_argument("command_name")
    p_which.set_defaults(func=cmd_which)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
