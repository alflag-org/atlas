"""Command-line interface for host-side Atlas operations."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from atlas_core.host import get_host

from .catalog import active_releases, command_index, resolve_command, resolve_job
from .config import load_config
from .errors import AtlasError
from .execution import execute
from .files import remove_path
from .job_instances import list_job_instances, load_job_instance
from .jobs import list_jobs, run_job, run_job_instance
from .launchers import (
    ensure_atlas_launcher,
    ensure_script_runner,
    regenerate_shims,
    sync_atlas_core,
)
from .manifests import load_manifest, validate_name
from .paths import ensure_dirs, get_paths
from .releases import install_release
from .runtime import install_runtime, runtime_status
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
    """Print host, release, command, and path status."""
    p = get_paths()
    ensure_dirs(p)
    config_path = p.etc / "config.yml"
    host_path = p.etc / "host.yml"
    releases = active_releases(p.scripts_current_root)
    commands = command_index(p.scripts_current_root) if releases else {}
    host_name = "unknown"
    if host_path.exists():
        try:
            host_name = get_host(str(host_path)).name
        except (FileNotFoundError, TypeError, ValueError):
            host_name = "unknown"
    print(f"config file path: {config_path}")
    print(f"host file path: {host_path}")
    print(f"host name: {host_name}")
    print(f"scripts current root: {p.scripts_current_root}")
    print(f"active releases count: {len(releases)}")
    for release in releases:
        print(f"release: {release.name} {release.version} {release.root}")
    print(f"commands count: {len(commands)}")
    print(f"jobs count: {sum(len(release.manifest.jobs) for release in releases)}")
    print(f"python scripts path: {p.scripts_python}")
    print(f"shims path: {p.shims}")
    return 0


def cmd_runtime_status(_: argparse.Namespace) -> int:
    """Print scripts runtime status."""
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
    """Install or replace the scripts runtime."""
    p = get_paths()
    ensure_dirs(p)
    cfg = load_config(p.etc / "config.yml")
    configured = cfg.runtime.python_version
    scripts_roots = [release.root for release in active_releases(p.scripts_current_root)]
    scripts = install_runtime(
        p.runtime,
        configured,
        scripts_roots or None,
        tmp_dir=p.tmp,
        python_build_cache_path=p.cache / "python-build",
    )
    print(f"installed scripts python: {scripts}")
    print(f"configured python version: {configured}")
    return 0


def cmd_scripts_install(args: argparse.Namespace) -> int:
    """Install one scripts release from a source argument."""
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
    release_name = load_manifest(source).name
    if args.name is not None and validate_name(args.name, kind="release") != release_name:
        raise ValueError(f"release name mismatch: {args.name} != {release_name}")
    snapshots = _capture_current_targets(p.scripts_current_root, [release_name])
    try:
        install_release(source, p.scripts_releases_root, p.scripts_current_root)
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
    """Update configured scripts releases."""
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
            manifest_name = load_manifest(source).name
            if manifest_name != release_name:
                raise ValueError(f"release name mismatch: {release_name} != {manifest_name}")
            install_release(source, p.scripts_releases_root, p.scripts_current_root)
        sync_atlas_core(p.home)
        ensure_atlas_launcher(p.bin_dir / "atlas")
        ensure_script_runner(p.script_runner, p.bin_dir / "atlas")
        regenerate_shims(p.scripts_current_root, p.shims, p.script_runner)
    except Exception:
        _restore_current_targets(p.scripts_current_root, snapshots)
        raise
    return 0


def cmd_scripts_list(args: argparse.Namespace) -> int:
    """List manifest-declared commands across active releases."""
    p = get_paths()
    for name, command in command_index(p.scripts_current_root).items():
        if args.verbose:
            print(
                f"{name}\t{command.release.name}\t{command.release.version}\t"
                f"{command.artifact.entrypoint}"
            )
        else:
            print(name)
    return 0


def cmd_scripts_shims(_: argparse.Namespace) -> int:
    """Regenerate shims for active release commands."""
    p = get_paths()
    ensure_script_runner(p.script_runner, p.bin_dir / "atlas")
    names = regenerate_shims(p.scripts_current_root, p.shims, p.script_runner)
    print(f"generated shims: {len(names)}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Run one public command through the shared executor."""
    p = get_paths()
    ensure_dirs(p)
    return execute(
        p,
        resolve_command(p.scripts_current_root, args.command_name),
        args.args,
    )


def cmd_which(args: argparse.Namespace) -> int:
    """Print the entrypoint for one command."""
    p = get_paths()
    print(resolve_command(p.scripts_current_root, args.command_name).artifact.entrypoint)
    return 0


def cmd_job_list(args: argparse.Namespace) -> int:
    """List non-public jobs."""
    for job in list_jobs(get_paths(), args.release):
        print(f"{job.release.name}\t{job.artifact.name}")
    return 0


def _job_data(release_name: str, job_name: str) -> dict[str, object]:
    paths = get_paths()
    job = resolve_job(paths.scripts_current_root, release_name, job_name)
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
    """List configured job instances whose jobs exist."""
    paths = get_paths()
    for instance in list_job_instances(paths.jobs_dir):
        resolve_job(paths.scripts_current_root, instance.release, instance.job)
        print(instance.name)
    return 0


def _instance_data(name: str) -> dict[str, object]:
    paths = get_paths()
    instance = load_job_instance(paths.jobs_dir, name)
    resolve_job(paths.scripts_current_root, instance.release, instance.job)
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


def build_parser() -> argparse.ArgumentParser:
    """Build the Atlas argument parser."""
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
    p_scripts_install.add_argument("--name")
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

    p_job = sub.add_parser("job")
    job_sub = p_job.add_subparsers(dest="job_cmd", required=True)
    p_job_list = job_sub.add_parser("list")
    p_job_list.add_argument("release", nargs="?")
    p_job_list.set_defaults(func=cmd_job_list)
    p_job_inspect = job_sub.add_parser("inspect")
    p_job_inspect.add_argument("release")
    p_job_inspect.add_argument("job")
    p_job_inspect.set_defaults(func=cmd_job_inspect)
    p_job_run = job_sub.add_parser("run")
    p_job_run.add_argument("release")
    p_job_run.add_argument("job")
    p_job_run.add_argument("args", nargs=argparse.REMAINDER)
    p_job_run.set_defaults(func=cmd_job_run)
    p_job_instance = job_sub.add_parser("instance")
    instance_sub = p_job_instance.add_subparsers(dest="instance_cmd", required=True)
    p_instance_list = instance_sub.add_parser("list")
    p_instance_list.set_defaults(func=cmd_job_instance_list)
    p_instance_inspect = instance_sub.add_parser("inspect")
    p_instance_inspect.add_argument("instance")
    p_instance_inspect.set_defaults(func=cmd_job_instance_inspect)
    p_instance_run = instance_sub.add_parser("run")
    p_instance_run.add_argument("instance")
    p_instance_run.set_defaults(func=cmd_job_instance_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the Atlas CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except AtlasError as exc:
        print(f"atlas: {exc}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
