"""Command-line interface for the small Atlas execution standard."""

from __future__ import annotations

import argparse
import json
import sys

from atlas_core.host import get_host

from .catalog import command_index, resolve_command
from .config import AtlasConfig, ProgramConfig, load_config
from .environments import create_environment, environment_executable, environment_path
from .execution import execute
from .paths import AtlasPaths, ensure_dirs, get_paths
from .runtime import (
    install_configured_runtimes,
    runtime_status,
)
from .shims import generate_shims


def _config(paths: AtlasPaths) -> AtlasConfig:
    return load_config(paths.config_file)


def _program(config: AtlasConfig, name: str) -> ProgramConfig:
    program = config.programs.get(name)
    if program is None:
        raise ValueError(f"unknown program: {name}")
    return program


def cmd_status(_: argparse.Namespace) -> int:
    """Print the current local program and Atlas path state."""
    paths = get_paths()
    config = _config(paths)
    commands = command_index(config)
    host_id = "unavailable"
    try:
        host_id = get_host(paths.host_file).id
    except (FileNotFoundError, TypeError, ValueError):
        pass
    print(f"config file: {paths.config_file}")
    print(f"host file: {paths.host_file}")
    print(f"host id: {host_id}")
    print(f"programs: {len(config.programs)}")
    print(f"commands: {len(commands)}")
    print(f"runtimes: {paths.runtimes}")
    print(f"environments: {paths.environments}")
    print(f"shims: {paths.shims}")
    print(f"run log: {paths.run_log}")
    return 0


def cmd_check(_: argparse.Namespace) -> int:
    """Check configured programs, runtimes, environments, and host identity."""
    paths = get_paths()
    config = _config(paths)
    failures: list[str] = []
    try:
        commands = command_index(config)
    except (OSError, ValueError) as exc:
        commands = {}
        failures.append(str(exc))
    try:
        get_host(paths.host_file)
    except (FileNotFoundError, TypeError, ValueError) as exc:
        failures.append(str(exc))
    for status in runtime_status(paths, config):
        if not status.available:
            failures.append(f"{status.type} runtime unavailable: {status.version}")
    for program in config.programs.values():
        if program.runtime.type != "native":
            try:
                environment_executable(paths, config, program)
            except (FileNotFoundError, PermissionError, ValueError) as exc:
                failures.append(str(exc))
    if failures:
        for failure in failures:
            print(f"atlas check: {failure}", file=sys.stderr)
        return 2
    print(f"ok: {len(config.programs)} programs, {len(commands)} commands")
    return 0


def cmd_secret_check(args: argparse.Namespace) -> int:
    """Verify required logical names without displaying their values."""
    from atlas_core.secrets import load_provider

    provider = load_provider()
    values = provider.get_many(args.names)
    print(f"secrets: {len(values)}/{len(set(args.names))} available")
    return 0


def cmd_context(_: argparse.Namespace) -> int:
    """Print the host and Atlas path context as JSON."""
    paths = get_paths()
    from .context import base_context

    print(json.dumps(base_context(paths), ensure_ascii=False, sort_keys=True))
    return 0


def cmd_program_list(args: argparse.Namespace) -> int:
    """List registered local programs."""
    config = _config(get_paths())
    for program in config.programs.values():
        if args.verbose:
            runtime = program.runtime.type
            if program.runtime.environment is not None:
                runtime += f" environment={program.runtime.environment}"
            print(f"{program.name}\t{program.root}\t{runtime}")
        else:
            print(program.name)
    return 0


def cmd_command_list(args: argparse.Namespace) -> int:
    """List discovered public commands."""
    commands = command_index(_config(get_paths()))
    for name, command in commands.items():
        if args.verbose:
            print(f"{name}\t{command.program.name}\t{command.type}\t{command.path}")
        else:
            print(name)
    return 0


def cmd_which(args: argparse.Namespace) -> int:
    """Print the local executable selected for one command."""
    command = resolve_command(_config(get_paths()), args.command_name)
    print(command.path)
    return 0


def cmd_shim_generate(_: argparse.Namespace) -> int:
    """Generate shims for all discovered commands."""
    paths = get_paths()
    ensure_dirs(paths)
    names = generate_shims(paths, _config(paths))
    for name in names:
        print(paths.shims / name)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Run one command through the shared executor."""
    paths = get_paths()
    ensure_dirs(paths)
    config = _config(paths)
    command = resolve_command(config, args.command_name)
    command_args = args.args[1:] if args.args[:1] == ["--"] else args.args
    return execute(paths, config, command, command_args, timeout_seconds=args.timeout)


def cmd_runtime_status(_: argparse.Namespace) -> int:
    """Print selected interpreter status."""
    paths = get_paths()
    config = _config(paths)
    for status in runtime_status(paths, config):
        executable = "unavailable" if status.executable is None else str(status.executable)
        print(f"{status.type}\t{status.version}\t{str(status.available).lower()}\t{status.atlas_path}\t{executable}")
    return 0


def cmd_runtime_install(_: argparse.Namespace) -> int:
    """Register configured interpreters below the Atlas runtime path."""
    paths = get_paths()
    ensure_dirs(paths)
    for path in install_configured_runtimes(paths, _config(paths)):
        print(path)
    return 0


def cmd_env_list(_: argparse.Namespace) -> int:
    """List configured dependency environments and validate existing ones."""
    paths = get_paths()
    config = _config(paths)
    for program in config.programs.values():
        if program.runtime.type == "native":
            continue
        path = environment_path(paths, program)
        state = "missing"
        if path.exists() or path.is_symlink():
            try:
                environment_executable(paths, config, program)
                state = "ready"
            except (OSError, RuntimeError, ValueError):
                state = "invalid"
        print(f"{program.name}\t{program.runtime.type}\t{path}\t{state}")
    return 0


def cmd_env_create(args: argparse.Namespace) -> int:
    """Create an empty dependency environment for one registered program."""
    paths = get_paths()
    ensure_dirs(paths)
    config = _config(paths)
    print(create_environment(paths, config, _program(config, args.program)))
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the minimal Atlas CLI."""
    parser = argparse.ArgumentParser(prog="atlas")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, function in (("status", cmd_status), ("check", cmd_check), ("context", cmd_context)):
        command = sub.add_parser(name)
        command.set_defaults(func=function)

    secrets = sub.add_parser("secret")
    secrets_sub = secrets.add_subparsers(dest="secret_command", required=True)
    secret_check = secrets_sub.add_parser("check")
    secret_check.add_argument("names", nargs="+")
    secret_check.set_defaults(func=cmd_secret_check)

    program = sub.add_parser("program")
    program_sub = program.add_subparsers(dest="program_command", required=True)
    program_list = program_sub.add_parser("list")
    program_list.add_argument("--verbose", action="store_true")
    program_list.set_defaults(func=cmd_program_list)

    command = sub.add_parser("command")
    command_sub = command.add_subparsers(dest="command_command", required=True)
    command_list = command_sub.add_parser("list")
    command_list.add_argument("--verbose", action="store_true")
    command_list.set_defaults(func=cmd_command_list)

    which = sub.add_parser("which")
    which.add_argument("command_name")
    which.set_defaults(func=cmd_which)

    shim = sub.add_parser("shim")
    shim_sub = shim.add_subparsers(dest="shim_command", required=True)
    shim_generate = shim_sub.add_parser("generate")
    shim_generate.set_defaults(func=cmd_shim_generate)

    run = sub.add_parser("run")
    run.add_argument("--timeout", type=int)
    run.add_argument("command_name")
    run.add_argument("args", nargs=argparse.REMAINDER)
    run.set_defaults(func=cmd_run)

    runtime = sub.add_parser("runtime")
    runtime_sub = runtime.add_subparsers(dest="runtime_command", required=True)
    runtime_status = runtime_sub.add_parser("status")
    runtime_status.set_defaults(func=cmd_runtime_status)
    runtime_install = runtime_sub.add_parser("install")
    runtime_install.set_defaults(func=cmd_runtime_install)

    env = sub.add_parser("env")
    env_sub = env.add_subparsers(dest="env_command", required=True)
    env_list = env_sub.add_parser("list")
    env_list.set_defaults(func=cmd_env_list)
    env_create = env_sub.add_parser("create")
    env_create.add_argument("program")
    env_create.set_defaults(func=cmd_env_create)
    from .cluster.cli import add_parser as add_cluster_parser
    from .daemon.cli import add_parser as add_daemon_parser

    add_daemon_parser(sub)
    add_cluster_parser(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run Atlas and convert domain errors into a concise diagnostic."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"atlas: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
