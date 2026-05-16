from __future__ import annotations

import argparse
from pathlib import Path

from atlas_core.host import get_host

from .commands import discover_commands
from .config import load_config
from .launchers import ensure_atlas_launcher, ensure_script_runner, regenerate_shims, sync_atlas_core
from .paths import ensure_dirs, get_paths
from .runner import resolve_command_path, run_command
from .releases import install_release, read_version
from .runtime import install_runtime, runtime_status
from .sources import resolve_source


def _bool_text(value: bool) -> str:
    return str(value).lower()


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
        try:
            host_name = get_host(str(host_path)).name
        except (FileNotFoundError, ValueError):
            host_name = "unknown"
    print(f"config file path: {config_path}")
    print(f"host file path: {host_path}")
    print(f"host name: {host_name}")
    print(f"scripts current path: {p.scripts}")
    print(f"scripts version: {version}")
    print(f"commands count: {count}")
    print(f"python scripts path: {p.scripts_python}")
    print(f"shims path: {p.shims}")
    return 0


def cmd_runtime_status(_: argparse.Namespace) -> int:
    p = get_paths()
    config_path = p.etc / "config.yml"
    configured = None
    if config_path.exists():
        cfg = load_config(config_path)
        configured = cfg.runtime.python_version
    st = runtime_status(p.runtime, configured)
    print("python:")
    print(f"  provider: {st.provider}")
    if st.configured_version is not None:
        print(f"  configured version: {st.configured_version}")
    print(f"  provider available: {_bool_text(st.provider_available)}")
    if st.pyenv_python is not None:
        print(f"  pyenv python: {st.pyenv_python}")
    elif st.pyenv_python_error is not None:
        print(f"  pyenv python error: {st.pyenv_python_error}")
    print(f"  scripts venv: {st.scripts_venv}")
    print(f"  scripts python: {st.scripts_python}")
    print(f"  scripts python exists: {_bool_text(st.scripts_python_exists)}")
    return 0


def cmd_runtime_install(_: argparse.Namespace) -> int:
    p = get_paths()
    ensure_dirs(p)
    cfg = load_config(p.etc / "config.yml")
    configured = cfg.runtime.python_version
    scripts = install_runtime(p.runtime, configured, p.scripts if p.scripts.exists() else None)
    print(f"installed scripts python: {scripts}")
    print(f"configured python version: {configured}")
    return 0


def _scripts_paths():
    p = get_paths()
    releases_root = p.home / "scripts" / "releases"
    current_link = p.home / "scripts" / "current"
    return p, releases_root, current_link


def cmd_scripts_install(args: argparse.Namespace) -> int:
    p, releases_root, current_link = _scripts_paths()
    ensure_dirs(p)
    config_path = p.etc / "config.yml"
    source_arg = args.source.strip()
    local_arg = Path(source_arg[7:]) if source_arg.startswith("file://") else Path(source_arg)
    needs_registry_config = (
        config_path.exists()
        and not local_arg.exists()
        and not source_arg.startswith(("git+", "http://", "https://"))
    )
    config = load_config(config_path) if needs_registry_config else None
    source = resolve_source(args.source, config=config, cache_dir=p.cache)
    install_release(source, releases_root, current_link)
    sync_atlas_core(p.home)
    ensure_atlas_launcher(p.bin_dir / "atlas")
    ensure_script_runner(p.script_runner, p.bin_dir / "atlas")
    names = regenerate_shims(current_link / "commands", p.shims, p.script_runner)
    print(f"installed scripts: {current_link}")
    print(f"commands: {len(names)}")
    return 0


def cmd_scripts_update(_: argparse.Namespace) -> int:
    p, releases_root, current_link = _scripts_paths()
    cfg = load_config(p.etc / "config.yml")
    source = resolve_source(cfg.scripts.source, config=cfg, cache_dir=p.cache)
    install_release(source, releases_root, current_link)
    sync_atlas_core(p.home)
    ensure_atlas_launcher(p.bin_dir / "atlas")
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
