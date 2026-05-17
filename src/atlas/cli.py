from __future__ import annotations

import argparse
from pathlib import Path

from atlas_core.host import get_host

from .config import load_config
from .files import remove_path
from .launchers import ensure_atlas_launcher, ensure_script_runner, regenerate_shims, sync_atlas_core
from .paths import ensure_dirs, get_paths
from .runner import resolve_command_path, run_command
from .releases import install_named_release
from .runtime import install_runtime, runtime_status
from .scriptsets import active_releases, build_command_index, discover_release_commands, validate_release_name
from .sources import resolve_source


def _bool_text(value: bool) -> str:
    return str(value).lower()


def _capture_current_targets(current_root: Path, release_names: list[str]) -> dict[str, Path | None]:
    snapshots: dict[str, Path | None] = {}
    for release_name in release_names:
        current_link = current_root / release_name
        if not current_link.exists() and not current_link.is_symlink():
            snapshots[release_name] = None
            continue
        if not current_link.is_symlink():
            raise ValueError(f"scripts current entry must be a symlink: {current_link}")
        target = current_link.resolve()
        if not target.exists() or not target.is_dir():
            raise ValueError(f"active release target not found: {current_link}")
        snapshots[release_name] = target
    return snapshots


def _restore_current_targets(current_root: Path, snapshots: dict[str, Path | None]) -> None:
    for release_name, target in snapshots.items():
        current_link = current_root / release_name
        if current_link.exists() or current_link.is_symlink():
            remove_path(current_link)
        if target is not None:
            current_link.symlink_to(target, target_is_directory=True)


def cmd_status(_: argparse.Namespace) -> int:
    p = get_paths()
    ensure_dirs(p)
    config_path = p.etc / "config.yml"
    host_path = p.etc / "host.yml"
    releases = active_releases(p.scripts_current_root)
    count = len(build_command_index(p.scripts_current_root)) if releases else 0
    host_name = "unknown"
    if host_path.exists():
        try:
            host_name = get_host(str(host_path)).name
        except (FileNotFoundError, ValueError):
            host_name = "unknown"
    print(f"config file path: {config_path}")
    print(f"host file path: {host_path}")
    print(f"host name: {host_name}")
    print(f"scripts current root: {p.scripts_current_root}")
    print(f"active releases count: {len(releases)}")
    for release in releases:
        print(f"release: {release.name} {release.version} {release.root}")
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
    scripts_roots = [release.root for release in active_releases(p.scripts_current_root)]
    scripts = install_runtime(p.runtime, configured, scripts_roots or None)
    print(f"installed scripts python: {scripts}")
    print(f"configured python version: {configured}")
    return 0


def cmd_scripts_install(args: argparse.Namespace) -> int:
    p = get_paths()
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
    release_name = validate_release_name(args.name)
    snapshots = _capture_current_targets(p.scripts_current_root, [release_name])
    try:
        install_named_release(source, p.scripts_releases_root, p.scripts_current_root, release_name)
        sync_atlas_core(p.home)
        ensure_atlas_launcher(p.bin_dir / "atlas")
        ensure_script_runner(p.script_runner, p.bin_dir / "atlas")
        names = regenerate_shims(p.scripts_current_root, p.shims, p.script_runner)
    except Exception:
        _restore_current_targets(p.scripts_current_root, snapshots)
        raise
    print(f"installed scripts: {p.scripts_current_root / release_name}")
    print(f"commands: {len(names)}")
    return 0


def cmd_scripts_update(args: argparse.Namespace) -> int:
    p = get_paths()
    ensure_dirs(p)
    cfg = load_config(p.etc / "config.yml")
    configured_releases = cfg.scripts.releases
    release_names = [args.release_name] if args.release_name else [name for name, release in configured_releases.items() if release.enabled]
    snapshots = _capture_current_targets(p.scripts_current_root, release_names)
    try:
        for release_name in release_names:
            if release_name not in configured_releases:
                raise ValueError(f"scripts release is not configured: {release_name}")
            release = configured_releases[release_name]
            source = resolve_source(release.source, config=cfg, cache_dir=p.cache)
            install_named_release(source, p.scripts_releases_root, p.scripts_current_root, release_name)
        sync_atlas_core(p.home)
        ensure_atlas_launcher(p.bin_dir / "atlas")
        ensure_script_runner(p.script_runner, p.bin_dir / "atlas")
        regenerate_shims(p.scripts_current_root, p.shims, p.script_runner)
    except Exception:
        _restore_current_targets(p.scripts_current_root, snapshots)
        raise
    return 0


def cmd_scripts_list(args: argparse.Namespace) -> int:
    p = get_paths()
    if args.verbose:
        build_command_index(p.scripts_current_root)
        for entry in discover_release_commands(p.scripts_current_root):
            print(f"{entry.name}\t{entry.release_name}\t{entry.release_version}\t{entry.script_path}")
        return 0
    for name in build_command_index(p.scripts_current_root):
        print(name)
    return 0


def cmd_scripts_shims(_: argparse.Namespace) -> int:
    p = get_paths()
    ensure_script_runner(p.script_runner, p.bin_dir / "atlas")
    names = regenerate_shims(p.scripts_current_root, p.shims, p.script_runner)
    print(f"generated shims: {len(names)}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    p = get_paths()
    ensure_dirs(p)
    return run_command(p, args.command_name, args.args)


def cmd_which(args: argparse.Namespace) -> int:
    p = get_paths()
    print(resolve_command_path(p.scripts_current_root, args.command_name))
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
    p_scripts_install.add_argument("--name", default="default")
    p_scripts_install.set_defaults(func=cmd_scripts_install)
    p_scripts_update = scripts_sub.add_parser("update")
    p_scripts_update.add_argument("release_name", nargs="?")
    p_scripts_update.set_defaults(func=cmd_scripts_update)
    p_scripts_list = scripts_sub.add_parser("list")
    p_scripts_list.add_argument("--verbose", action="store_true")
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
