from __future__ import annotations

import json
import os
import pwd
import signal
import subprocess
import time
from pathlib import Path

import pytest

from atlas.catalog import resolve_command, resolve_job
from atlas.cli import main
from atlas.errors import LockUnavailableError
from atlas.execution import (
    _append_run_log,
    _forward_termination_signal,
    _terminate_process_group,
    execute,
    git_context,
    redact_args,
)
from atlas.job_instances import list_job_instances, load_job_instance
from atlas.jobs import list_jobs, run_job, run_job_instance
from atlas.launchers import (
    ensure_atlas_launcher,
    ensure_script_runner,
    regenerate_shims,
)
from atlas.locks import acquire_lock
from atlas.releases import install_release


def _activate(paths, source: Path) -> None:
    install_release(
        source,
        paths.scripts_releases_root,
        paths.scripts_current_root,
    )


def _last_log(paths) -> dict[str, object]:
    line = (paths.logs / "runs.jsonl").read_text(encoding="utf-8").strip().splitlines()[-1]
    value = json.loads(line)
    assert isinstance(value, dict)
    return value


def _all_logs(paths) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in (paths.logs / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    ]


def test_execute_sets_environment_and_logs_correlation(
    atlas_paths,
    release_factory,
    monkeypatch: pytest.MonkeyPatch,
    capfd,
) -> None:
    source = release_factory(name="sample", commands=("sample-show",))
    modules = source / "modules"
    modules.mkdir()
    other = release_factory(name="other", commands=())
    (other / "modules").mkdir()
    _activate(atlas_paths, source)
    _activate(atlas_paths, other)
    monkeypatch.setenv("ATLAS_RUN_ID", "parent-run")
    monkeypatch.setenv("ATLAS_OPERATION_ID", "root-operation")
    monkeypatch.setenv("PYTHONPATH", "/existing")
    command = resolve_command(atlas_paths.scripts_current_root, "sample-show")
    atlas_paths.var.mkdir(parents=True)

    assert execute(
        atlas_paths,
        command,
        ["--token", "secret", "KEY=value"],
        cwd=atlas_paths.var,
    ) == 0

    stdout, stderr = capfd.readouterr()
    assert "sample-show:sample" in stdout
    assert "$ sample-show --token '***' 'KEY=***'" in stderr
    assert "secret" not in stderr
    assert "KEY=value" not in stderr
    record = _last_log(atlas_paths)
    assert record["parent_run_id"] == "parent-run"
    assert record["operation_id"] == "root-operation"
    assert record["artifact_type"] == "command"
    assert record["artifact"] == "sample-show"
    assert record["args"] == ["--token", "***", "KEY=***"]
    assert record["git_root"] is None
    assert record["timed_out"] is False
    assert record["lock"] is None


def test_execute_root_run_has_own_operation_id(atlas_paths, release_factory) -> None:
    source = release_factory()
    _activate(atlas_paths, source)
    command = resolve_command(atlas_paths.scripts_current_root, "sample-show")

    assert execute(atlas_paths, command, []) == 0

    record = _last_log(atlas_paths)
    assert record["parent_run_id"] is None
    assert record["operation_id"] == record["run_id"]


def test_execute_handles_empty_caller_path(
    atlas_paths,
    release_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = release_factory()
    _activate(atlas_paths, source)
    monkeypatch.delenv("PATH", raising=False)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    command = resolve_command(atlas_paths.scripts_current_root, "sample-show")
    assert execute(atlas_paths, command, []) == 0


def test_execute_orders_path_and_preserves_caller_environment(
    atlas_paths,
    release_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = release_factory(name="selected")
    (source / "modules").mkdir()
    other = release_factory(name="other", commands=())
    (other / "modules").mkdir()
    _activate(atlas_paths, source)
    _activate(atlas_paths, other)
    monkeypatch.setenv("PATH", "/caller/bin")
    monkeypatch.setenv("PYTHONPATH", "/caller/python")
    captured: dict[str, object] = {}

    class Finished:
        pid = 123

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return Finished()

    monkeypatch.setattr(
        "atlas.execution.git_context",
        lambda cwd: {
            "git_root": None,
            "git_commit": None,
            "git_dirty": None,
            "git_branch": None,
        },
    )
    monkeypatch.setattr("atlas.execution.subprocess.Popen", fake_popen)
    command = resolve_command(atlas_paths.scripts_current_root, "sample-show")

    assert execute(atlas_paths, command, []) == 0

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["PATH"].split(os.pathsep) == [
        str(atlas_paths.shims),
        str(atlas_paths.scripts_python.parent),
        "/caller/bin",
    ]
    assert env["PYTHONPATH"].split(os.pathsep) == [
        str((atlas_paths.scripts_current_root / "selected").resolve() / "modules"),
        str((atlas_paths.scripts_current_root / "other").resolve() / "modules"),
        str(atlas_paths.home / "lib/python"),
        "/caller/python",
    ]
    assert captured["start_new_session"] is True
    assert captured["text"] is True


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["--password=x"], ["--password=***"]),
        (["--api-key", "x"], ["--api-key", "***"]),
        (["--monkey", "x"], ["--monkey", "***"]),
        (["DB_PASSWORD=x"], ["DB_PASSWORD=***"]),
        (["plain"], ["plain"]),
    ],
)
def test_redact_args(args: list[str], expected: list[str]) -> None:
    assert redact_args(args) == expected


def test_git_context_reports_clean_dirty_detached_and_non_git(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    (repo / "tracked").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "tracked"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)

    clean = git_context(repo)
    assert clean["git_root"] == str(repo)
    assert clean["git_dirty"] is False
    assert clean["git_branch"] in {"master", "main"}
    assert isinstance(clean["git_commit"], str)

    (repo / "tracked").write_text("two\n", encoding="utf-8")
    assert git_context(repo)["git_dirty"] is True
    subprocess.run(["git", "-C", str(repo), "checkout", "--detach", "-q"], check=True)
    assert git_context(repo)["git_branch"] is None
    assert git_context(tmp_path / "missing") == {
        "git_root": None,
        "git_commit": None,
        "git_dirty": None,
        "git_branch": None,
    }


def test_execute_reads_environment_files_and_runs_job_instance(
    atlas_paths,
    release_factory,
    capfd,
) -> None:
    source = release_factory(name="worker", commands=(), jobs=("collect",), timeout=60)
    _activate(atlas_paths, source)
    workdir = atlas_paths.var / "work"
    workdir.mkdir(parents=True)
    environment = atlas_paths.env_dir / "worker.env"
    environment.parent.mkdir()
    environment.write_text(
        "# comment\nTEST_JOB_VALUE='from file'\nEMPTY=\n",
        encoding="utf-8",
    )
    atlas_paths.jobs_dir.mkdir()
    user = pwd.getpwuid(os.geteuid()).pw_name
    (atlas_paths.jobs_dir / "worker-collect.yml").write_text(
        "schema: atlas.job-instance/v1\n"
        "release: worker\n"
        "job: collect\n"
        f"user: {user}\n"
        f"working_directory: {workdir}\n"
        "arguments:\n"
        "  - --site\n"
        "  - default\n"
        "environment_files:\n"
        f"  - {environment}\n"
        "timeout_seconds: 30\n"
        "lock: worker-collect\n",
        encoding="utf-8",
    )

    assert [item.name for item in list_job_instances(atlas_paths.jobs_dir)] == ["worker-collect"]
    instance = load_job_instance(atlas_paths.jobs_dir, "worker-collect")
    assert instance.arguments == ("--site", "default")
    assert run_job_instance(atlas_paths, "worker-collect") == 0

    stdout, _ = capfd.readouterr()
    assert "from file" in stdout
    assert "--site|default" in stdout
    record = _last_log(atlas_paths)
    assert record["artifact_type"] == "job"
    assert record["cwd"] == str(workdir)
    assert record["timeout"] == 30
    assert record["lock"] == "worker-collect"


def test_run_log_rejects_symlinks_and_non_files(
    atlas_paths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    atlas_paths.logs.parent.mkdir(parents=True, exist_ok=True)
    real_logs = tmp_path / "real-logs"
    real_logs.mkdir()
    atlas_paths.logs.symlink_to(real_logs, target_is_directory=True)
    with pytest.raises(ValueError, match="logs path must be a directory"):
        _append_run_log(atlas_paths, {"run_id": "sample"})

    atlas_paths.logs.unlink()
    atlas_paths.logs.write_text("bad", encoding="utf-8")
    with pytest.raises(ValueError, match="logs path must be a directory"):
        _append_run_log(atlas_paths, {"run_id": "sample"})

    atlas_paths.logs.unlink()
    atlas_paths.logs.mkdir()
    target = tmp_path / "target"
    target.write_text("do not append", encoding="utf-8")
    (atlas_paths.logs / "runs.jsonl").symlink_to(target)
    with pytest.raises(ValueError, match="run log must be a regular file"):
        _append_run_log(atlas_paths, {"run_id": "sample"})

    (atlas_paths.logs / "runs.jsonl").unlink()
    read_descriptor, write_descriptor = os.pipe()
    monkeypatch.setattr("atlas.execution.os.open", lambda *args, **kwargs: write_descriptor)
    try:
        with pytest.raises(ValueError, match="run log must be a regular file"):
            _append_run_log(atlas_paths, {"run_id": "sample"})
    finally:
        os.close(read_descriptor)


def test_direct_job_inherits_cwd_and_passthrough(
    atlas_paths,
    release_factory,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd,
) -> None:
    source = release_factory(name="worker", commands=(), jobs=("collect",))
    _activate(atlas_paths, source)
    monkeypatch.chdir(tmp_path)
    assert [job.artifact.name for job in list_jobs(atlas_paths)] == ["collect"]
    assert [job.artifact.name for job in list_jobs(atlas_paths, "worker")] == ["collect"]
    assert run_job(atlas_paths, "worker", "collect", ["one", "two"]) == 0
    assert "one|two" in capfd.readouterr().out
    assert _last_log(atlas_paths)["cwd"] == str(tmp_path)
    with pytest.raises(ValueError, match="unknown release"):
        list_jobs(atlas_paths, "missing")
    with pytest.raises(ValueError, match="unknown release"):
        resolve_job(atlas_paths.scripts_current_root, "missing", "collect")
    with pytest.raises(ValueError, match="unknown job"):
        resolve_job(atlas_paths.scripts_current_root, "worker", "missing")


def test_job_instance_uses_job_default_timeout(
    atlas_paths,
    release_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = release_factory(name="worker", commands=(), jobs=("collect",), timeout=42)
    _activate(atlas_paths, source)
    workdir = atlas_paths.var / "work"
    workdir.mkdir(parents=True)
    atlas_paths.jobs_dir.mkdir()
    user = pwd.getpwuid(os.geteuid()).pw_name
    (atlas_paths.jobs_dir / "default-timeout.yml").write_text(
        "schema: atlas.job-instance/v1\n"
        "release: worker\n"
        "job: collect\n"
        f"user: {user}\n"
        f"working_directory: {workdir}\n",
        encoding="utf-8",
    )
    calls: list[dict[str, object]] = []

    def fake_execute(*args, **kwargs):
        calls.append(kwargs)
        return 0

    monkeypatch.setattr("atlas.jobs.execute", fake_execute)
    assert run_job_instance(atlas_paths, "default-timeout") == 0
    assert calls[0]["timeout_seconds"] == 42
    assert calls[0]["lock"] == "default-timeout"


def test_job_instance_rejects_user_mismatch(atlas_paths, release_factory) -> None:
    source = release_factory(name="worker", commands=(), jobs=("collect",))
    _activate(atlas_paths, source)
    workdir = atlas_paths.var / "work"
    workdir.mkdir(parents=True)
    atlas_paths.jobs_dir.mkdir()
    (atlas_paths.jobs_dir / "wrong-user.yml").write_text(
        "schema: atlas.job-instance/v1\n"
        "release: worker\n"
        "job: collect\n"
        "user: certainly-not-current\n"
        f"working_directory: {workdir}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="does not match caller"):
        run_job_instance(atlas_paths, "wrong-user")


@pytest.mark.parametrize(
    ("body", "expected_exception", "message"),
    [
        ("[]\n", TypeError, "must be a mapping"),
        (
            "schema: wrong\nrelease: x\njob: x\nuser: x\nworking_directory: /tmp\n",
            ValueError,
            "unsupported job instance schema",
        ),
        (
            "schema: atlas.job-instance/v1\nrelease: x\njob: x\nuser: x\n"
            "working_directory: relative\n",
            ValueError,
            "working_directory must be absolute",
        ),
        (
            "schema: atlas.job-instance/v1\nrelease: x\njob: x\nuser: x\n"
            "working_directory: /tmp\narguments: x\n",
            ValueError,
            r"arguments must be a list\[str\]",
        ),
        (
            "schema: atlas.job-instance/v1\nrelease: x\njob: x\nuser: x\n"
            "working_directory: /tmp\nenvironment_files: [relative]\n",
            ValueError,
            "environment_files must contain absolute",
        ),
        (
            "schema: atlas.job-instance/v1\nrelease: x\njob: x\nworking_directory: /tmp\n",
            ValueError,
            "user is required",
        ),
        (
            "schema: atlas.job-instance/v1\nrelease: x\njob: x\nuser: x\n"
            "working_directory: /tmp\nenvironment_files: [1]\n",
            ValueError,
            r"environment_files must be a list\[str\]",
        ),
        (
            "schema: atlas.job-instance/v1\nrelease: x\njob: x\nuser: x\n"
            "working_directory: /tmp\ntimeout_seconds: 0\n",
            ValueError,
            "timeout_seconds must be a positive",
        ),
        (
            "schema: atlas.job-instance/v1\nrelease: x\njob: x\nuser: x\n"
            "working_directory: /tmp\nlock: 1\n",
            TypeError,
            "lock must be a string",
        ),
        (
            "schema: atlas.job-instance/v1\nrelease: x\njob: x\nuser: x\n"
            "working_directory: /tmp\nextra: true\n",
            ValueError,
            "has unknown key",
        ),
    ],
)
def test_job_instance_validation(
    atlas_paths,
    body: str,
    expected_exception: type[Exception],
    message: str,
) -> None:
    atlas_paths.jobs_dir.mkdir(exist_ok=True)
    (atlas_paths.jobs_dir / "sample.yml").write_text(body, encoding="utf-8")
    with pytest.raises(expected_exception, match=message):
        load_job_instance(atlas_paths.jobs_dir, "sample")


def test_job_instance_directory_and_symlink_fail_closed(atlas_paths, tmp_path: Path) -> None:
    assert list_job_instances(atlas_paths.jobs_dir) == []
    atlas_paths.jobs_dir.parent.mkdir(exist_ok=True)
    atlas_paths.jobs_dir.write_text("bad", encoding="utf-8")
    with pytest.raises(ValueError, match="jobs directory must be a directory"):
        list_job_instances(atlas_paths.jobs_dir)
    atlas_paths.jobs_dir.unlink()
    atlas_paths.jobs_dir.mkdir()
    target = tmp_path / "target.yml"
    target.write_text("{}\n", encoding="utf-8")
    (atlas_paths.jobs_dir / "linked.yml").symlink_to(target)
    with pytest.raises(ValueError, match="job instance file not found"):
        load_job_instance(atlas_paths.jobs_dir, "linked")

    (atlas_paths.jobs_dir / "linked.yml").unlink()
    (atlas_paths.jobs_dir / "directory.yml").mkdir()
    with pytest.raises(ValueError, match="job instance file not found"):
        load_job_instance(atlas_paths.jobs_dir, "directory")
    (atlas_paths.jobs_dir / "directory.yml").rmdir()
    atlas_paths.jobs_dir.rmdir()
    real = tmp_path / "real-jobs"
    real.mkdir()
    atlas_paths.jobs_dir.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="jobs directory must be a directory"):
        list_job_instances(atlas_paths.jobs_dir)
    with pytest.raises(ValueError, match="jobs directory must be a directory"):
        load_job_instance(atlas_paths.jobs_dir, "sample")


def test_environment_file_validation(atlas_paths, release_factory, tmp_path: Path) -> None:
    source = release_factory(name="worker", commands=(), jobs=("collect",))
    _activate(atlas_paths, source)
    job = resolve_job(atlas_paths.scripts_current_root, "worker", "collect")
    with pytest.raises(ValueError, match="must be absolute"):
        execute(atlas_paths, job, [], environment_files=(Path("relative"),))
    with pytest.raises(ValueError, match="not found"):
        execute(atlas_paths, job, [], environment_files=(tmp_path / "missing",))
    invalid = tmp_path / "invalid.env"
    invalid.write_text("not an assignment\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid environment assignment"):
        execute(atlas_paths, job, [], environment_files=(invalid,))


def test_advisory_lock_conflict_and_cli_exit(
    atlas_paths,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    with acquire_lock(atlas_paths.locks, "shared-lock") as path:
        assert path.name == "shared-lock.lock"
        with pytest.raises(LockUnavailableError, match="already held"):
            with acquire_lock(atlas_paths.locks, "shared-lock"):
                pass
    with pytest.raises(ValueError, match="invalid lock name"):
        with acquire_lock(atlas_paths.locks, "Bad"):
            pass

    def unavailable(_paths, _instance):
        raise LockUnavailableError("lock is already held: sample")

    monkeypatch.setattr("atlas.cli.run_job_instance", unavailable)
    assert main(["job", "instance", "run", "sample"]) == 75
    assert "atlas: lock is already held: sample" in capsys.readouterr().err


def test_advisory_lock_rejects_symlinks_and_non_files(atlas_paths, tmp_path: Path) -> None:
    atlas_paths.locks.parent.mkdir(parents=True, exist_ok=True)
    real_locks = tmp_path / "real-locks"
    real_locks.mkdir()
    atlas_paths.locks.symlink_to(real_locks, target_is_directory=True)
    with pytest.raises(ValueError, match="locks path must be a directory"):
        with acquire_lock(atlas_paths.locks, "sample"):
            pass

    atlas_paths.locks.unlink()
    atlas_paths.locks.write_text("bad", encoding="utf-8")
    with pytest.raises(ValueError, match="locks path must be a directory"):
        with acquire_lock(atlas_paths.locks, "sample"):
            pass

    atlas_paths.locks.unlink()
    atlas_paths.locks.mkdir()
    target = tmp_path / "target"
    target.write_text("do not open", encoding="utf-8")
    (atlas_paths.locks / "sample.lock").symlink_to(target)
    with pytest.raises(ValueError, match="lock file must be a regular file"):
        with acquire_lock(atlas_paths.locks, "sample"):
            pass

    (atlas_paths.locks / "sample.lock").unlink()
    (atlas_paths.locks / "sample.lock").mkdir()
    with pytest.raises(ValueError, match="lock file must be a regular file"):
        with acquire_lock(atlas_paths.locks, "sample"):
            pass

    (atlas_paths.locks / "sample.lock").rmdir()
    os.mkfifo(atlas_paths.locks / "sample.lock")
    with pytest.raises(ValueError, match="lock file must be a regular file"):
        with acquire_lock(atlas_paths.locks, "sample"):
            pass


def test_execute_timeout_terminates_process_group(atlas_paths, release_factory) -> None:
    source = release_factory(name="worker", commands=(), jobs=("slow-job",))
    (source / "jobs/slow-job.py").write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    _activate(atlas_paths, source)
    job = resolve_job(atlas_paths.scripts_current_root, "worker", "slow-job")
    started = time.monotonic()

    assert execute(atlas_paths, job, [], timeout_seconds=1) == 124

    assert time.monotonic() - started < 8
    record = _last_log(atlas_paths)
    assert record["timed_out"] is True
    assert record["timeout"] == 1


def test_execute_normalizes_signal_exit(atlas_paths, release_factory) -> None:
    source = release_factory(name="signals", commands=("signal-stop",))
    (source / "commands/signal-stop.py").write_text(
        "import os, signal\nos.kill(os.getpid(), signal.SIGTERM)\n",
        encoding="utf-8",
    )
    _activate(atlas_paths, source)
    command = resolve_command(atlas_paths.scripts_current_root, "signal-stop")
    assert execute(atlas_paths, command, []) == 128 + signal.SIGTERM


def test_execute_validates_runtime_cwd_and_timeout(
    atlas_paths,
    release_factory,
    tmp_path: Path,
) -> None:
    source = release_factory()
    _activate(atlas_paths, source)
    command = resolve_command(atlas_paths.scripts_current_root, "sample-show")
    with pytest.raises(ValueError, match="working directory not found"):
        execute(atlas_paths, command, [], cwd=tmp_path / "missing")
    with pytest.raises(ValueError, match="timeout must be a positive"):
        execute(atlas_paths, command, [], timeout_seconds=0)
    atlas_paths.scripts_python.unlink()
    with pytest.raises(ValueError, match="scripts python executable not found"):
        execute(atlas_paths, command, [])


def test_execute_reports_popen_missing_after_precheck(
    atlas_paths,
    release_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = release_factory()
    _activate(atlas_paths, source)
    command = resolve_command(atlas_paths.scripts_current_root, "sample-show")

    def missing(*args, **kwargs):
        raise FileNotFoundError("gone")

    monkeypatch.setattr("atlas.execution.subprocess.Popen", missing)
    with pytest.raises(ValueError, match="runtime executable not found"):
        execute(atlas_paths, command, [])


def test_process_group_termination_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    class Finished:
        pid = 10

        def poll(self):
            return 0

    _terminate_process_group(Finished())

    class Running:
        pid = 11

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(
        "atlas.execution.os.killpg",
        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError),
    )
    _terminate_process_group(Running())

    signals: list[int] = []

    class NeedsKill:
        pid = 12
        waits = 0

        def poll(self):
            return None

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("child", timeout)
            return 0

    process = NeedsKill()

    def kill_then_missing(pid, sig):
        signals.append(sig)
        if sig == signal.SIGKILL:
            raise ProcessLookupError

    monkeypatch.setattr("atlas.execution.os.killpg", kill_then_missing)
    _terminate_process_group(process)
    assert signals == [signal.SIGTERM, signal.SIGKILL]

    signals.clear()
    process = NeedsKill()
    monkeypatch.setattr("atlas.execution.os.killpg", lambda pid, sig: signals.append(sig))
    _terminate_process_group(process)
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert process.waits == 2


def test_termination_signal_is_forwarded_and_handler_is_restored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous_handler = object()
    handlers: list[object] = []
    forwarded: list[tuple[int, int]] = []

    monkeypatch.setattr("atlas.execution.signal.getsignal", lambda signum: previous_handler)
    monkeypatch.setattr(
        "atlas.execution.signal.signal",
        lambda signum, handler: handlers.append(handler),
    )
    monkeypatch.setattr(
        "atlas.execution.os.killpg",
        lambda pid, signum: forwarded.append((pid, signum)),
    )

    class Running:
        pid = 42

    with _forward_termination_signal(Running()):
        handler = handlers[-1]
        assert callable(handler)
        handler(signal.SIGTERM, None)

    assert forwarded == [(42, signal.SIGTERM)]
    assert handlers[-1] is previous_handler

    monkeypatch.setattr(
        "atlas.execution.os.killpg",
        lambda pid, signum: (_ for _ in ()).throw(ProcessLookupError),
    )
    with _forward_termination_signal(Running()):
        handler = handlers[-1]
        assert callable(handler)
        handler(signal.SIGTERM, None)


def test_execute_logs_and_reraises_keyboard_interrupt(
    atlas_paths,
    release_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = release_factory()
    _activate(atlas_paths, source)
    command = resolve_command(atlas_paths.scripts_current_root, "sample-show")

    class Interrupted:
        pid = 123

        def wait(self, timeout=None):
            raise KeyboardInterrupt

        def poll(self):
            return 0

    monkeypatch.setattr(
        "atlas.execution.subprocess.Popen",
        lambda *args, **kwargs: Interrupted(),
    )
    monkeypatch.setattr(
        "atlas.execution.git_context",
        lambda cwd: {
            "git_root": None,
            "git_commit": None,
            "git_dirty": None,
            "git_branch": None,
        },
    )
    with pytest.raises(KeyboardInterrupt):
        execute(atlas_paths, command, [])
    assert _last_log(atlas_paths)["exit_code"] == 130


def test_cli_job_commands_and_instances(atlas_paths, release_factory, capsys) -> None:
    source = release_factory(name="worker", commands=(), jobs=("collect",), timeout=15)
    _activate(atlas_paths, source)
    workdir = atlas_paths.var / "work"
    workdir.mkdir(parents=True)
    atlas_paths.jobs_dir.mkdir()
    user = pwd.getpwuid(os.geteuid()).pw_name
    (atlas_paths.jobs_dir / "worker-collect.yml").write_text(
        "schema: atlas.job-instance/v1\n"
        "release: worker\n"
        "job: collect\n"
        f"user: {user}\n"
        f"working_directory: {workdir}\n",
        encoding="utf-8",
    )

    assert main(["job", "list"]) == 0
    assert "worker\tcollect" in capsys.readouterr().out
    assert main(["job", "list", "worker"]) == 0
    assert main(["job", "inspect", "worker", "collect"]) == 0
    assert "default_timeout_seconds: 15" in capsys.readouterr().out
    assert main(["job", "run", "worker", "collect", "--", "one"]) == 0
    assert main(["job", "run", "worker", "collect", "two"]) == 0
    assert main(["job", "instance", "list"]) == 0
    assert "worker-collect" in capsys.readouterr().out
    assert main(["job", "instance", "inspect", "worker-collect"]) == 0
    assert "schema: atlas.job-instance/v1" in capsys.readouterr().out
    assert main(["job", "instance", "run", "worker-collect"]) == 0

    ensure_script_runner(atlas_paths.script_runner, atlas_paths.bin_dir / "atlas")
    assert regenerate_shims(
        atlas_paths.scripts_current_root,
        atlas_paths.shims,
        atlas_paths.script_runner,
    ) == []
    assert not (atlas_paths.shims / "collect").exists()


def test_nested_shim_execution_preserves_operation_and_parent(
    atlas_paths,
    release_factory,
) -> None:
    source = release_factory(name="nested", commands=("parent", "child"))
    (source / "commands/parent.py").write_text(
        "import subprocess\n"
        "raise SystemExit(subprocess.run(['child'], check=False).returncode)\n",
        encoding="utf-8",
    )
    _activate(atlas_paths, source)
    ensure_atlas_launcher(atlas_paths.bin_dir / "atlas")
    ensure_script_runner(atlas_paths.script_runner, atlas_paths.bin_dir / "atlas")
    regenerate_shims(
        atlas_paths.scripts_current_root,
        atlas_paths.shims,
        atlas_paths.script_runner,
    )

    parent = resolve_command(atlas_paths.scripts_current_root, "parent")
    assert execute(atlas_paths, parent, []) == 0

    records = {record["artifact"]: record for record in _all_logs(atlas_paths)}
    assert records["parent"]["parent_run_id"] is None
    assert records["child"]["parent_run_id"] == records["parent"]["run_id"]
    assert records["child"]["operation_id"] == records["parent"]["operation_id"]
