"""Job and job-instance application services."""

from __future__ import annotations

import os
import pwd

from .catalog import ExecutableRef, active_releases, resolve_job
from .execution import execute
from .job_instances import JobInstance, load_job_instance
from .paths import AtlasPaths


def list_jobs(paths: AtlasPaths, release_name: str | None = None) -> list[ExecutableRef]:
    """List jobs across active releases or within one release."""
    refs: list[ExecutableRef] = []
    found_release = release_name is None
    for release in active_releases(paths.current_root, paths.releases_root):
        if release_name is not None and release.name != release_name:
            continue
        found_release = True
        refs.extend(
            ExecutableRef(release=release, artifact_type="job", artifact=job)
            for job in release.manifest.jobs.values()
        )
    if not found_release:
        raise ValueError(f"unknown release: {release_name}")
    return refs


def run_job(paths: AtlasPaths, release_name: str, job_name: str, args: list[str]) -> int:
    """Run one direct job in the caller's current working directory."""
    return execute(
        paths,
        lambda: resolve_job(paths.current_root, paths.releases_root, release_name, job_name),
        args,
        timeout_seconds=lambda job: job.artifact.default_timeout_seconds,
    )


def _validate_caller_user(instance: JobInstance) -> None:
    caller = pwd.getpwuid(os.geteuid()).pw_name
    if caller != instance.user:
        raise ValueError(
            f"job instance user {instance.user} does not match caller user {caller}; "
            "Atlas does not invoke sudo implicitly"
        )


def run_job_instance(paths: AtlasPaths, name: str) -> int:
    """Resolve and execute one ``jobs.d`` instance."""
    instance = load_job_instance(paths.jobs_dir, name)
    _validate_caller_user(instance)
    def resolve_instance_job():
        job = resolve_job(
            paths.current_root,
            paths.releases_root,
            instance.release,
            instance.job,
        )
        return job

    return execute(
        paths,
        resolve_instance_job,
        list(instance.arguments),
        cwd=instance.working_directory,
        environment_files=instance.environment_files,
        timeout_seconds=lambda job: (
            instance.timeout_seconds
            if instance.timeout_seconds is not None
            else job.artifact.default_timeout_seconds
        ),
        lock=instance.lock,
    )
