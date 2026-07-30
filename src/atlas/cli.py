"""Command-line interface for Atlas host operations."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

import yaml

from atlas_core.host import get_host

from .catalog import (
    active_releases,
    command_index,
    release_index,
    resolve_command,
    resolve_job,
    resolve_service,
)
from .config import load_config
from .errors import AtlasError
from .execution import execute
from .files import remove_path
from .init import SystemdAdapter
from .job_instances import list_job_instances, load_job_instance
from .jobs import list_jobs, run_job, run_job_instance
from .launchers import (
    ensure_artifact_runner,
    ensure_atlas_launcher,
    regenerate_shims,
    sync_atlas_core,
)
from .paths import AtlasPaths, ensure_dirs, get_paths
from .releases import install_release, validate_release
from .runtime import install_runtime, runtime_status
from .sources import resolve_source


def _bool_text(value: bool) -> str:
    return str(value).lower()


def _capture_current_targets(current_root: Path, names: list[str]) -> dict[str, Path | None]:
    snapshots: dict[str, Path | None] = {}
    for name in names:
        link = current_root / name
        if not link.exists() and not link.is_symlink():
            snapshots[name] = None
            continue
        if not link.is_symlink():
            raise ValueError(f"current entry must be a symlink: {link}")
        target = link.resolve()
        if not target.is_dir():
            raise ValueError(f"active release target not found: {link}")
        snapshots[name] = target
    return snapshots


def _restore_current_targets(current_root: Path, snapshots: dict[str, Path | None]) -> None:
    for name, target in snapshots.items():
        link = current_root / name
        remove_path(link)
        if target is not None:
            link.symlink_to(target, target_is_directory=True)


def _refresh_host_artifacts(paths: AtlasPaths) -> list[str]:
    sync_atlas_core(paths.home)
    atlas_launcher = paths.bin_dir / "atlas"
    ensure_atlas_launcher(atlas_launcher)
    ensure_artifact_runner(paths.artifact_runner, atlas_launcher)
    return regenerate_shims(paths.current_root, paths.shims, paths.artifact_runner)


def cmd_status(_: argparse.Namespace) -> int:
    """Print current host and artifact status."""
    paths = get_paths()
    ensure_dirs(paths)
    releases = active_releases(paths.current_root)
    commands = command_index(paths.current_root)
    host_file = paths.etc / "host.yml"
    host_name = "unknown"
    if host_file.exists():
        try:
            host_name = get_host(host_file).name
        except (FileNotFoundError, ValueError):
            pass
    print(f"config file path: {paths.etc / 'config.yml'}")
    print(f"host file path: {host_file}")
    print(f"host name: {host_name}")
    print(f"current root: {paths.current_root}")
    print(f"active releases count: {len(releases)}")
    for release in releases:
        print(f"release: {release.name} {release.version} {release.root}")
    print(f"commands count: {len(commands)}")
    print(f"jobs count: {sum(len(release.manifest.jobs) for release in releases)}")
    print(f"services count: {sum(len(release.manifest.services) for release in releases)}")
    print(f"runtime python: {paths.runtime_python}")
    print(f"shims path: {paths.shims}")
    return 0


def cmd_runtime_status(_: argparse.Namespace) -> int:
    """Print artifact runtime status."""
    paths = get_paths()
    config_path = paths.etc / "config.yml"
    configured = load_config(config_path).runtime.python_version if config_path.exists() else None
    status = runtime_status(paths.runtime, configured)
    print("python:")
    print(f"  provider: {status.provider}")
    if status.configured_version is not None:
        print(f"  configured version: {status.configured_version}")
    print(f"  provider available: {_bool_text(status.provider_available)}")
    if status.pyenv_python is not None:
        print(f"  pyenv python: {status.pyenv_python}")
    elif status.pyenv_python_error is not None:
        print(f"  pyenv python error: {status.pyenv_python_error}")
    print(f"  artifacts venv: {status.artifacts_venv}")
    print(f"  runtime python: {status.runtime_python}")
    print(f"  runtime python exists: {_bool_text(status.runtime_python_exists)}")
    return 0


def cmd_runtime_install(_: argparse.Namespace) -> int:
    """Install or replace the artifact runtime."""
    paths = get_paths()
    ensure_dirs(paths)
    config = load_config(paths.etc / "config.yml")
    roots = [release.root for release in active_releases(paths.current_root)]
    runtime_python = install_runtime(
        paths.runtime,
        config.runtime.python_version,
        roots or None,
        tmp_dir=paths.tmp,
        python_build_cache_path=paths.cache / "python-build",
    )
    print(f"installed runtime python: {runtime_python}")
    print(f"configured python version: {config.runtime.python_version}")
    return 0


def cmd_release_install(args: argparse.Namespace) -> int:
    """Install one release using its manifest name."""
    paths = get_paths()
    ensure_dirs(paths)
    source = resolve_source(args.source, cache_dir=paths.cache)
    release = validate_release(source)
    snapshots = _capture_current_targets(paths.current_root, [release.manifest.name])
    try:
        install_release(source, paths.releases_root, paths.current_root)
        names = _refresh_host_artifacts(paths)
    except Exception:
        _restore_current_targets(paths.current_root, snapshots)
        raise
    print(f"installed release: {release.manifest.name} {release.version}")
    print(f"commands: {len(names)}")
    return 0


def cmd_release_update(args: argparse.Namespace) -> int:
    """Update configured releases transactionally at activation level."""
    paths = get_paths()
    ensure_dirs(paths)
    config = load_config(paths.etc / "config.yml")
    names = [args.release_name] if args.release_name else [
        name for name, release in config.releases.items() if release.enabled
    ]
    snapshots = _capture_current_targets(paths.current_root, names)
    try:
        for name in names:
            configured = config.releases.get(name)
            if configured is None:
                raise ValueError(f"release is not configured: {name}")
            source = resolve_source(configured.source, cache_dir=paths.cache)
            release = validate_release(source)
            if release.manifest.name != name:
                raise ValueError(
                    f"configured release name mismatch: {name} != {release.manifest.name}"
                )
            install_release(source, paths.releases_root, paths.current_root)
        _refresh_host_artifacts(paths)
    except Exception:
        _restore_current_targets(paths.current_root, snapshots)
        raise
    return 0


def cmd_release_list(args: argparse.Namespace) -> int:
    """List active releases."""
    for release in active_releases(get_paths().current_root):
        if args.verbose:
            print(
                f"{release.name}\t{release.version}\t{release.root}\t"
                f"commands={len(release.manifest.commands)}\t"
                f"jobs={len(release.manifest.jobs)}\t"
                f"services={len(release.manifest.services)}"
            )
        else:
            print(release.name)
    return 0


def cmd_release_shims(_: argparse.Namespace) -> int:
    """Regenerate command-only shims."""
    paths = get_paths()
    ensure_dirs(paths)
    names = _refresh_host_artifacts(paths)
    print(f"generated shims: {len(names)}")
    return 0


def cmd_command_list(args: argparse.Namespace) -> int:
    """List public commands."""
    for name, command in command_index(get_paths().current_root).items():
        if args.verbose:
            print(
                f"{name}\t{command.release.name}\t{command.release.version}\t"
                f"{command.artifact.entrypoint}"
            )
        else:
            print(name)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Run one public command."""
    paths = get_paths()
    ensure_dirs(paths)
    return execute(paths, resolve_command(paths.current_root, args.command_name), args.args)


def cmd_which(args: argparse.Namespace) -> int:
    """Print one command entrypoint."""
    command = resolve_command(get_paths().current_root, args.command_name)
    print(command.artifact.entrypoint)
    return 0


def cmd_job_list(args: argparse.Namespace) -> int:
    """List non-public jobs."""
    for job in list_jobs(get_paths(), args.release):
        print(f"{job.release.name}\t{job.artifact.name}")
    return 0


def _job_data(release_name: str, job_name: str) -> dict[str, object]:
    job = resolve_job(get_paths().current_root, release_name, job_name)
    return {
        "release": job.release.name,
        "version": job.release.version,
        "job": job.artifact.name,
        "runtime": job.artifact.runtime,
        "entrypoint": str(job.artifact.entrypoint),
        "default_timeout_seconds": job.artifact.default_timeout_seconds,
    }


def cmd_job_inspect(args: argparse.Namespace) -> int:
    """Print one job definition."""
    print(yaml.safe_dump(_job_data(args.release, args.job), sort_keys=False), end="")
    return 0


def cmd_job_run(args: argparse.Namespace) -> int:
    """Run one direct job."""
    artifact_args = args.args[1:] if args.args[:1] == ["--"] else args.args
    return run_job(get_paths(), args.release, args.job, artifact_args)


def cmd_job_instance_list(_: argparse.Namespace) -> int:
    """List configured job instances."""
    paths = get_paths()
    for instance in list_job_instances(paths.jobs_dir):
        resolve_job(paths.current_root, instance.release, instance.job)
        print(instance.name)
    return 0


def _instance_data(name: str) -> dict[str, object]:
    paths = get_paths()
    instance = load_job_instance(paths.jobs_dir, name)
    resolve_job(paths.current_root, instance.release, instance.job)
    return {
        "schema": "atlas.job-instance/v1",
        "release": instance.release,
        "job": instance.job,
        "user": instance.user,
        "working_directory": str(instance.working_directory),
        "arguments": list(instance.arguments),
        "environment_files": [str(path) for path in instance.environment_files],
        "timeout_seconds": instance.timeout_seconds,
        "lock": instance.lock,
    }


def cmd_job_instance_inspect(args: argparse.Namespace) -> int:
    """Print one job instance."""
    print(yaml.safe_dump(_instance_data(args.instance), sort_keys=False), end="")
    return 0


def cmd_job_instance_run(args: argparse.Namespace) -> int:
    """Run one job instance."""
    return run_job_instance(get_paths(), args.instance)


def cmd_init_list(args: argparse.Namespace) -> int:
    """List Atlas-owned init artifacts."""
    releases = release_index(get_paths().current_root)
    if args.release is not None and args.release not in releases:
        raise ValueError(f"unknown release: {args.release}")
    for release in releases.values():
        if args.release is not None and release.name != args.release:
            continue
        for service in release.manifest.services.values():
            print(f"{release.name}\t{service.name}\tsystemd")
    return 0


def cmd_init_diff(args: argparse.Namespace) -> int:
    """Print systemd unit differences."""
    paths = get_paths()
    service = resolve_service(paths.current_root, args.release, args.service)
    print(SystemdAdapter(jobs_dir=paths.jobs_dir).diff(service), end="")
    return 0


def cmd_init_install(args: argparse.Namespace) -> int:
    """Install Atlas-owned systemd artifacts."""
    paths = get_paths()
    service = resolve_service(paths.current_root, args.release, args.service)
    for path in SystemdAdapter(jobs_dir=paths.jobs_dir).install(service):
        print(path)
    return 0


def cmd_init_remove(args: argparse.Namespace) -> int:
    """Remove Atlas-owned systemd artifacts."""
    paths = get_paths()
    service = resolve_service(paths.current_root, args.release, args.service)
    for path in SystemdAdapter(jobs_dir=paths.jobs_dir).remove(service):
        print(path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the complete Atlas CLI parser."""
    parser = argparse.ArgumentParser(prog="atlas")
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status")
    status.set_defaults(func=cmd_status)

    runtime = sub.add_parser("runtime")
    runtime_sub = runtime.add_subparsers(dest="runtime_command", required=True)
    runtime_status_parser = runtime_sub.add_parser("status")
    runtime_status_parser.set_defaults(func=cmd_runtime_status)
    runtime_install_parser = runtime_sub.add_parser("install")
    runtime_install_parser.set_defaults(func=cmd_runtime_install)

    release = sub.add_parser("release")
    release_sub = release.add_subparsers(dest="release_command", required=True)
    release_install = release_sub.add_parser("install")
    release_install.add_argument("source")
    release_install.set_defaults(func=cmd_release_install)
    release_update = release_sub.add_parser("update")
    release_update.add_argument("release_name", nargs="?")
    release_update.set_defaults(func=cmd_release_update)
    release_list = release_sub.add_parser("list")
    release_list.add_argument("--verbose", action="store_true")
    release_list.set_defaults(func=cmd_release_list)
    release_shims = release_sub.add_parser("shims")
    release_shims.set_defaults(func=cmd_release_shims)

    command = sub.add_parser("command")
    command_sub = command.add_subparsers(dest="command_command", required=True)
    command_list = command_sub.add_parser("list")
    command_list.add_argument("--verbose", action="store_true")
    command_list.set_defaults(func=cmd_command_list)

    run = sub.add_parser("run")
    run.add_argument("command_name")
    run.add_argument("args", nargs=argparse.REMAINDER)
    run.set_defaults(func=cmd_run)
    which = sub.add_parser("which")
    which.add_argument("command_name")
    which.set_defaults(func=cmd_which)

    job = sub.add_parser("job")
    job_sub = job.add_subparsers(dest="job_command", required=True)
    job_list = job_sub.add_parser("list")
    job_list.add_argument("release", nargs="?")
    job_list.set_defaults(func=cmd_job_list)
    job_inspect = job_sub.add_parser("inspect")
    job_inspect.add_argument("release")
    job_inspect.add_argument("job")
    job_inspect.set_defaults(func=cmd_job_inspect)
    job_run = job_sub.add_parser("run")
    job_run.add_argument("release")
    job_run.add_argument("job")
    job_run.add_argument("args", nargs=argparse.REMAINDER)
    job_run.set_defaults(func=cmd_job_run)
    job_instance = job_sub.add_parser("instance")
    instance_sub = job_instance.add_subparsers(dest="instance_command", required=True)
    instance_list = instance_sub.add_parser("list")
    instance_list.set_defaults(func=cmd_job_instance_list)
    instance_inspect = instance_sub.add_parser("inspect")
    instance_inspect.add_argument("instance")
    instance_inspect.set_defaults(func=cmd_job_instance_inspect)
    instance_run = instance_sub.add_parser("run")
    instance_run.add_argument("instance")
    instance_run.set_defaults(func=cmd_job_instance_run)

    init = sub.add_parser("init")
    init_sub = init.add_subparsers(dest="init_command", required=True)
    init_list = init_sub.add_parser("list")
    init_list.add_argument("release", nargs="?")
    init_list.set_defaults(func=cmd_init_list)
    for action, function in (
        ("diff", cmd_init_diff),
        ("install", cmd_init_install),
        ("remove", cmd_init_remove),
    ):
        action_parser = init_sub.add_parser(action)
        action_parser.add_argument("release")
        action_parser.add_argument("service")
        action_parser.set_defaults(func=function)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run Atlas and render expected failures without tracebacks."""
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except AtlasError as error:
        print(f"atlas: {error}", file=sys.stderr)
        return error.exit_code
    except (FileNotFoundError, ValueError) as error:
        print(f"atlas: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
