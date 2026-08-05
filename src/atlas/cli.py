"""Command-line interface for host-side Atlas operations."""

from __future__ import annotations

import argparse
import shutil
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

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
from .init import SystemdAdapter
from .job_instances import list_job_instances, load_job_instance
from .jobs import list_jobs, run_job, run_job_instance
from .launchers import publish_host_artifacts
from .locks import acquire_lock
from .paths import AtlasPaths, ensure_dirs, get_paths
from .releases import (
    reversible_release_install,
    reversible_release_transaction,
    validate_release,
)
from .runtime import install_runtime, runtime_status
from .sources import resolve_source


def _bool_text(value: bool) -> str:
    return str(value).lower()


def _refresh_host_artifacts(paths: AtlasPaths) -> list[str]:
    return publish_host_artifacts(paths, _lock_held=True)


@contextmanager
def _host_artifact_transaction(paths: AtlasPaths):
    """Serialize host publication before per-release activation locks."""
    # Lock order is fixed: host-artifacts, then sorted release locks.
    with acquire_lock(paths.locks, "host-artifacts", wait=True):
        yield


def cmd_status(_: argparse.Namespace) -> int:
    """Print current host and artifact status."""
    paths = get_paths()
    ensure_dirs(paths)
    releases = active_releases(paths.current_root, paths.releases_root)
    commands = command_index(paths.current_root, paths.releases_root)
    host_file = paths.etc / "host.yml"
    host_name = "unknown"
    if host_file.exists():
        try:
            host_name = get_host(host_file).name
        except (FileNotFoundError, TypeError, ValueError):
            pass
    print(f"config file path: {paths.etc / 'config.yml'}")
    print(f"host file path: {host_file}")
    print(f"host name: {host_name}")
    print(f"releases root: {paths.releases_root}")
    print(f"current root: {paths.current_root}")
    print(f"active releases count: {len(releases)}")
    for release in releases:
        print(
            f"release: {release.name} {release.version} {release.content_digest} "
            f"{release.root}"
        )
    print(f"commands count: {len(commands)}")
    print(f"jobs count: {sum(len(release.manifest.jobs) for release in releases)}")
    print(f"services count: {sum(len(release.manifest.services) for release in releases)}")
    print(f"artifact runner: {paths.artifact_runner}")
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
    with _host_artifact_transaction(paths):
        roots = [
            release.root
            for release in active_releases(paths.current_root, paths.releases_root)
        ]
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
    config = load_config(paths.etc / "config.yml")
    source = resolve_source(args.source, cache_dir=paths.cache)
    with _host_artifact_transaction(paths):
        refresh_started = False
        try:
            with reversible_release_install(
                source,
                paths.releases_root,
                paths.current_root,
                runtime_root=paths.runtime,
                python_version=config.runtime.python_version,
                tmp_dir=paths.tmp,
                python_build_cache_path=paths.cache / "python-build",
            ) as target:
                refresh_started = True
                names = _refresh_host_artifacts(paths)
            release = validate_release(target, validate_targets=False)
        except Exception:
            if refresh_started:
                try:
                    _refresh_host_artifacts(paths)
                except Exception as rollback_error:
                    raise RuntimeError(
                        "release installation failed and host artifacts could not be restored"
                    ) from rollback_error
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
    names.sort()
    with ExitStack() as temporary_sources:
        sources: list[Path] = []
        for name in names:
            configured = config.releases.get(name)
            if configured is None:
                raise ValueError(f"release is not configured: {name}")
            source = resolve_source(configured.source, cache_dir=paths.cache)
            release = validate_release(source, validate_targets=False)
            if release.manifest.name != name:
                raise ValueError(
                    f"configured release name mismatch: {name} != {release.manifest.name}"
                )
            temporary_root = Path(
                temporary_sources.enter_context(
                    TemporaryDirectory(prefix="release-source.", dir=paths.tmp)
                )
            )
            temporary_source = temporary_root / name
            shutil.copytree(release.root, temporary_source)
            sources.append(temporary_source)
        with _host_artifact_transaction(paths):
            refresh_started = False
            try:
                with reversible_release_transaction(  # pragma: no branch - contextmanager entry arc
                    sources,
                    paths.releases_root,
                    paths.current_root,
                    runtime_root=paths.runtime,
                    python_version=config.runtime.python_version,
                    tmp_dir=paths.tmp,
                    python_build_cache_path=paths.cache / "python-build",
                ):
                    refresh_started = True
                    _refresh_host_artifacts(paths)
            except Exception:
                if refresh_started:
                    try:
                        _refresh_host_artifacts(paths)
                    except Exception as rollback_error:
                        raise RuntimeError(
                            "release update failed and host artifacts could not be restored"
                        ) from rollback_error
                raise
    return 0


def cmd_release_list(args: argparse.Namespace) -> int:
    """List active releases."""
    paths = get_paths()
    for release in active_releases(paths.current_root, paths.releases_root):
        if args.verbose:
            print(
                f"{release.name}\t{release.version}\t{release.root}\t"
                f"digest={release.content_digest}\t"
                f"commands={len(release.manifest.commands)}\t"
                f"jobs={len(release.manifest.jobs)}\t"
                f"services={len(release.manifest.services)}"
            )
        else:
            print(release.name)
    return 0


def cmd_command_list(args: argparse.Namespace) -> int:
    """List public commands."""
    paths = get_paths()
    for name, command in command_index(paths.current_root, paths.releases_root).items():
        if args.verbose:
            print(
                f"{name}\t{command.release.name}\t{command.release.version}\t"
                f"digest={command.release.content_digest}\t{command.artifact.target.spec}"
            )
        else:
            print(name)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Run one public command through the shared executor."""
    p = get_paths()
    ensure_dirs(p)
    return execute(
        p,
        resolve_command(p.current_root, p.releases_root, args.command_name),
        args.args,
    )


def cmd_which(args: argparse.Namespace) -> int:
    """Print the target for one command."""
    p = get_paths()
    print(
        resolve_command(p.current_root, p.releases_root, args.command_name)
        .artifact.target.spec
    )
    return 0


def cmd_job_list(args: argparse.Namespace) -> int:
    """List non-public jobs."""
    for job in list_jobs(get_paths(), args.release):
        print(f"{job.release.name}\t{job.artifact.name}")
    return 0


def _job_data(release_name: str, job_name: str) -> dict[str, object]:
    paths = get_paths()
    job = resolve_job(paths.current_root, paths.releases_root, release_name, job_name)
    return {
        "release": job.release.name,
        "version": job.release.version,
        "job": job.artifact.name,
        "target": job.artifact.target.spec,
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
        resolve_job(
            paths.current_root,
            paths.releases_root,
            instance.release,
            instance.job,
        )
        print(instance.name)
    return 0


def _instance_data(name: str) -> dict[str, object]:
    paths = get_paths()
    instance = load_job_instance(paths.jobs_dir, name)
    resolve_job(
        paths.current_root,
        paths.releases_root,
        instance.release,
        instance.job,
    )
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


def cmd_systemd_list(args: argparse.Namespace) -> int:
    """List Atlas-owned systemd services."""
    paths = get_paths()
    releases = release_index(paths.current_root, paths.releases_root)
    if args.release is not None and args.release not in releases:
        raise ValueError(f"unknown release: {args.release}")
    for release in releases.values():
        if args.release is not None and release.name != args.release:
            continue
        for service in release.manifest.services.values():
            print(f"{release.name}\t{service.name}\tsystemd")
    return 0


def cmd_systemd_diff(args: argparse.Namespace) -> int:
    """Print systemd unit differences."""
    paths = get_paths()
    service = resolve_service(
        paths.current_root,
        paths.releases_root,
        args.release,
        args.service,
    )
    print(SystemdAdapter(jobs_dir=paths.jobs_dir).diff(service), end="")
    return 0


def cmd_systemd_install(args: argparse.Namespace) -> int:
    """Install Atlas-owned systemd artifacts."""
    paths = get_paths()
    service = resolve_service(
        paths.current_root,
        paths.releases_root,
        args.release,
        args.service,
    )
    for path in SystemdAdapter(jobs_dir=paths.jobs_dir).install(service):
        print(path)
    return 0


def cmd_systemd_remove(args: argparse.Namespace) -> int:
    """Remove Atlas-owned systemd artifacts."""
    paths = get_paths()
    service = resolve_service(
        paths.current_root,
        paths.releases_root,
        args.release,
        args.service,
    )
    for path in SystemdAdapter(jobs_dir=paths.jobs_dir).remove(service):
        print(path)
    return 0


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

    p_release = sub.add_parser("release")
    release_sub = p_release.add_subparsers(dest="release_cmd", required=True)
    p_release_install = release_sub.add_parser("install")
    p_release_install.add_argument("source")
    p_release_install.set_defaults(func=cmd_release_install)
    p_release_update = release_sub.add_parser("update")
    p_release_update.add_argument("release_name", nargs="?")
    p_release_update.set_defaults(func=cmd_release_update)
    p_release_list = release_sub.add_parser("list")
    p_release_list.add_argument("--verbose", action="store_true")
    p_release_list.set_defaults(func=cmd_release_list)
    p_command = sub.add_parser("command")
    command_sub = p_command.add_subparsers(dest="command_cmd", required=True)
    p_command_list = command_sub.add_parser("list")
    p_command_list.add_argument("--verbose", action="store_true")
    p_command_list.set_defaults(func=cmd_command_list)

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

    p_systemd = sub.add_parser("systemd")
    systemd_sub = p_systemd.add_subparsers(dest="systemd_cmd", required=True)
    p_systemd_list = systemd_sub.add_parser("list")
    p_systemd_list.add_argument("release", nargs="?")
    p_systemd_list.set_defaults(func=cmd_systemd_list)
    for action, function in (
        ("diff", cmd_systemd_diff),
        ("install", cmd_systemd_install),
        ("remove", cmd_systemd_remove),
    ):
        action_parser = systemd_sub.add_parser(action)
        action_parser.add_argument("release")
        action_parser.add_argument("service")
        action_parser.set_defaults(func=function)

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
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"atlas: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
