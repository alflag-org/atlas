"""Run one command inside a kernel-backed Linux cgroup.

The parent process creates and verifies the cgroup before starting this
launcher.  The launcher waits for the parent to attach it to that cgroup,
then forks and execs the requested command.  This removes the fork/attach
race and gives parent-death handling a single, shared implementation for the
Atlas executor and release host adapters.
"""

from __future__ import annotations

import ctypes
import os
import select
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

_CGROUP_MOUNT = Path("/sys/fs/cgroup")
_PARENT_DEATH_SIGNAL = signal.SIGTERM
_PR_SET_PDEATHSIG = 1
_TERMINATE_GRACE_SECONDS = 5.0
_LAUNCH_HANDSHAKE_SECONDS = 30.0
_CONTAINMENT_EXIT_CODE = 125


class ContainmentError(RuntimeError):
    """The command could not be, or could no longer be, safely contained."""


class ContainmentUnavailable(ContainmentError):
    """The required delegated Linux cgroup interface is unavailable."""


def _control(path: Path, name: str) -> Path:
    control = path / name
    if control.is_symlink() or not control.exists():
        raise ContainmentUnavailable(f"cgroup control is unavailable: {control}")
    return control


def _write_control(path: Path, name: str, value: str) -> None:
    control = _control(path, name)
    descriptor = -1
    try:
        descriptor = os.open(
            control,
            os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        os.write(descriptor, value.encode("ascii"))
    except OSError as exc:
        raise ContainmentUnavailable(f"cannot write cgroup control: {control}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_pids(path: Path) -> set[int]:
    try:
        control = _control(path, "cgroup.procs")
        values = control.read_text(encoding="ascii").splitlines()
    except OSError as exc:
        raise ContainmentError(f"cannot read cgroup membership: {path / 'cgroup.procs'}") from exc
    pids: set[int] = set()
    for value in values:
        if not value:
            continue
        if not value.isdecimal() or int(value) <= 0:
            raise ContainmentError(f"invalid cgroup membership: {control}")
        pids.add(int(value))
    return pids


def _validated_cgroup(path: Path) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise ContainmentUnavailable(f"cgroup directory is unavailable: {path}")
    try:
        resolved_mount = _CGROUP_MOUNT.resolve(strict=True)
        resolved = path.resolve(strict=True)
        resolved.relative_to(resolved_mount)
    except (OSError, ValueError) as exc:
        raise ContainmentUnavailable(f"cgroup directory is outside the cgroup mount: {path}") from exc
    _control(path, "cgroup.controllers")
    _control(path, "cgroup.procs")
    _control(path, "cgroup.kill")
    return path


def _current_cgroup() -> Path:
    try:
        records = Path("/proc/self/cgroup").read_text(encoding="ascii").splitlines()
    except OSError as exc:
        raise ContainmentUnavailable("Linux cgroup membership cannot be read") from exc
    for record in records:
        fields = record.split(":", 2)
        if len(fields) == 3 and fields[0] == "0":
            relative = fields[2]
            parts = tuple(part for part in relative.split("/") if part)
            if any(part in {".", ".."} for part in parts):
                raise ContainmentUnavailable("current cgroup path is unsafe")
            return _validated_cgroup(_CGROUP_MOUNT.joinpath(*parts))
    raise ContainmentUnavailable("Linux cgroup v2 membership is unavailable")


@dataclass
class ProcessContainment:
    """One cgroup v2 directory and its process membership operations."""

    path: Path
    attached: bool = False
    termination_started: bool = False

    @classmethod
    def create(cls) -> ProcessContainment:
        if sys.platform != "linux":
            raise ContainmentUnavailable("Atlas process containment requires Linux cgroup v2")
        parent = _current_cgroup()
        path = parent / f"atlas-exec-{uuid4().hex}"
        try:
            path.mkdir(mode=0o700)
        except OSError as exc:
            raise ContainmentUnavailable(f"cannot create delegated cgroup: {path}") from exc
        try:
            _validated_cgroup(path)
        except BaseException:
            try:
                path.rmdir()
            except OSError:
                pass
            raise
        return cls(path)

    @classmethod
    def from_path(cls, path: Path) -> ProcessContainment:
        """Open a cgroup path passed by the trusted parent launcher."""
        return cls(_validated_cgroup(path))

    def members(self, *, skip: set[int] | None = None) -> set[int]:
        ignored = set() if skip is None else skip
        return _read_pids(self.path) - ignored

    def attach(self, pid: int) -> None:
        if self.attached:
            raise ContainmentError(f"process cgroup is already attached: {self.path}")
        _write_control(self.path, "cgroup.procs", str(pid))
        self.attached = True
        if pid not in self.members():
            raise ContainmentError(f"process was not retained by cgroup: {self.path}")

    def signal_members(self, signum: int, *, skip: set[int] | None = None) -> None:
        for pid in self.members(skip=skip):
            try:
                os.kill(pid, signum)
            except ProcessLookupError:
                continue
            except OSError as exc:
                raise ContainmentError(
                    f"cannot signal cgroup member {pid} in {self.path}"
                ) from exc

    def kill(self) -> None:
        _write_control(self.path, "cgroup.kill", "1")

    def terminate(
        self,
        process: subprocess.Popen[str],
        *,
        initial_signal: int = signal.SIGTERM,
    ) -> None:
        """Terminate the whole cgroup with one five-second deadline."""
        if self.termination_started:
            raise ContainmentError(
                f"cgroup termination was already attempted: {self.path}"
            )
        self.termination_started = True
        self.signal_members(initial_signal)
        deadline = time.monotonic() + _TERMINATE_GRACE_SECONDS
        while True:
            members = self.members()
            if process.poll() is not None and not members:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                process.wait(timeout=min(0.05, remaining))
            except subprocess.TimeoutExpired:
                continue
        self.kill()
        try:
            process.wait()
        except OSError as exc:
            raise ContainmentError(f"contained process did not terminate: {self.path}") from exc
        if self.members():
            raise ContainmentError(f"cgroup still has members after SIGKILL: {self.path}")

    def cleanup(self, process: subprocess.Popen[str]) -> None:
        """Remove an empty cgroup, terminating residual descendants first."""
        if self.members():
            if getattr(self, "termination_started", False):
                raise ContainmentError(
                    f"cgroup still has members after termination attempt: {self.path}"
                )
            self.terminate(process)
        try:
            self.path.rmdir()
        except OSError as exc:
            raise ContainmentError(f"cannot remove execution cgroup: {self.path}") from exc


@dataclass
class ContainedProcess:
    """The parent-side process handle and its kernel containment."""

    process: subprocess.Popen[str]
    containment: ProcessContainment

    def terminate(self, *, initial_signal: int = signal.SIGTERM) -> None:
        self.containment.terminate(self.process, initial_signal=initial_signal)

    def cleanup(self) -> None:
        self.containment.cleanup(self.process)


def _abort_launch(
    process: subprocess.Popen[str] | None,
    containment: ProcessContainment,
) -> None:
    if process is not None:
        try:
            if containment.attached:
                containment.kill()
            elif process.poll() is None:
                process.kill()
            process.wait()
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        if containment.members():
            containment.kill()
    except ContainmentError:
        pass
    try:
        containment.path.rmdir()
    except OSError:
        pass


def _read_ready(
    descriptor: int,
    process: subprocess.Popen[str],
    *,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    buffer = b""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ContainmentError("process supervisor handshake timed out")
        readable, _, _ = select.select([descriptor], [], [], min(0.1, remaining))
        if not readable:
            if process.poll() is not None:
                raise ContainmentError("process supervisor exited before attachment")
            continue
        chunk = os.read(descriptor, 128)
        if not chunk:
            raise ContainmentError("process supervisor closed its handshake")
        buffer += chunk
        if b"\n" not in buffer:
            continue
        line = buffer.split(b"\n", 1)[0]
        try:
            ready_pid = int(line.decode("ascii"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ContainmentError("process supervisor sent an invalid handshake") from exc
        if ready_pid != process.pid:
            raise ContainmentError("process supervisor handshake PID mismatch")
        return


def spawn_contained(
    argv: list[str],
    *,
    python_executable: Path | str = sys.executable,
    supervisor_path: Path | str | None = None,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    stdin: int | object | None = None,
    stdout: int | object | None = None,
    stderr: int | object | None = None,
    handshake_timeout_seconds: float = _LAUNCH_HANDSHAKE_SECONDS,
) -> ContainedProcess:
    """Start ``argv`` only after a supervisor is attached to a cgroup."""
    if not argv:
        raise ValueError("contained argv must not be empty")
    if sys.platform != "linux":
        raise ContainmentUnavailable("Atlas process containment requires Linux cgroup v2")
    supervisor = (
        Path(__file__).resolve() if supervisor_path is None else Path(supervisor_path)
    )
    if supervisor.is_symlink() or not supervisor.is_file():
        raise ContainmentUnavailable(f"process supervisor is unavailable: {supervisor}")
    containment = ProcessContainment.create()
    ready_read, ready_write = os.pipe()
    go_read, go_write = os.pipe()
    process: subprocess.Popen[str] | None = None
    child_env = os.environ.copy() if env is None else dict(env)
    child_env.update(
        {
            "ATLAS_SUPERVISOR_READY_FD": str(ready_write),
            "ATLAS_SUPERVISOR_GO_FD": str(go_read),
            "ATLAS_SUPERVISOR_CGROUP": str(containment.path),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    command = [str(python_executable), str(supervisor), "--", *argv]
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=child_env,
            text=True,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
            pass_fds=(ready_write, go_read),
            shell=False,
        )
        os.close(ready_write)
        ready_write = -1
        os.close(go_read)
        go_read = -1
        _read_ready(
            ready_read,
            process,
            timeout_seconds=handshake_timeout_seconds,
        )
        containment.attach(process.pid)
        os.write(go_write, b"1")
        os.close(go_write)
        go_write = -1
        return ContainedProcess(process, containment)
    except BaseException:
        _abort_launch(process, containment)
        raise
    finally:
        for descriptor in (ready_read, ready_write, go_read, go_write):
            if descriptor >= 0:
                os.close(descriptor)


def _set_parent_death_signal() -> None:
    parent_pid = os.getppid()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_PDEATHSIG, _PARENT_DEATH_SIGNAL, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise ContainmentError(f"cannot set supervisor parent-death signal: errno {error}")
    if os.getppid() != parent_pid:
        os.kill(os.getpid(), _PARENT_DEATH_SIGNAL)


def _supervisor_exit(status: int) -> None:
    if os.WIFEXITED(status):
        os._exit(os.WEXITSTATUS(status))
    if os.WIFSIGNALED(status):
        signum = os.WTERMSIG(status)
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
    os._exit(_CONTAINMENT_EXIT_CODE)


def _run_supervisor(argv: list[str]) -> int:
    ready_fd_text = os.environ.get("ATLAS_SUPERVISOR_READY_FD")
    go_fd_text = os.environ.get("ATLAS_SUPERVISOR_GO_FD")
    cgroup_text = os.environ.get("ATLAS_SUPERVISOR_CGROUP")
    if not ready_fd_text or not go_fd_text or not cgroup_text:
        return _CONTAINMENT_EXIT_CODE
    try:
        ready_fd = int(ready_fd_text)
        go_fd = int(go_fd_text)
        containment = ProcessContainment.from_path(Path(cgroup_text))
        _set_parent_death_signal()
    except (ContainmentError, OSError, ValueError) as exc:
        print(f"process containment unavailable: {exc}", file=sys.stderr)
        return _CONTAINMENT_EXIT_CODE

    stop_requested = False
    child_pid: int | None = None

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError as exc:
                raise ContainmentError(
                    f"cannot signal contained child {child_pid}"
                ) from exc

    signal.signal(signal.SIGTERM, request_stop)
    signal.siginterrupt(signal.SIGTERM, True)
    try:
        os.write(ready_fd, f"{os.getpid()}\n".encode("ascii"))
        if os.read(go_fd, 1) != b"1":
            return _CONTAINMENT_EXIT_CODE
    except OSError:
        return _CONTAINMENT_EXIT_CODE
    finally:
        os.close(ready_fd)
        os.close(go_fd)

    try:
        child_pid = os.fork()
    except OSError as exc:
        print(f"process supervisor could not fork: {exc}", file=sys.stderr)
        return _CONTAINMENT_EXIT_CODE
    if child_pid == 0:
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
            signal.signal(signum, signal.SIG_DFL)
        try:
            os.execvpe(argv[0], argv, os.environ)
        except FileNotFoundError:
            print(f"{argv[0]} command not found", file=sys.stderr)
            os._exit(127)
        except OSError as exc:
            print(f"cannot execute {argv[0]}: {exc}", file=sys.stderr)
            os._exit(126)

    try:
        deadline: float | None = None
        status: int | None = None
        while status is None:  # pragma: no branch - a waited child exits via break
            try:
                waited, child_status = os.waitpid(child_pid, os.WNOHANG)
            except InterruptedError:
                waited = 0
            if waited:
                status = child_status
                break
            if stop_requested:
                if deadline is None:
                    containment.signal_members(signal.SIGTERM, skip={os.getpid()})
                    deadline = time.monotonic() + _TERMINATE_GRACE_SECONDS
                elif time.monotonic() >= deadline:
                    containment.kill()
                    return _CONTAINMENT_EXIT_CODE
            time.sleep(0.01)

        if not stop_requested:
            while containment.members(skip={os.getpid()}):
                if stop_requested:
                    break
                time.sleep(0.01)

        if stop_requested:
            if deadline is None:
                deadline = time.monotonic() + _TERMINATE_GRACE_SECONDS
                containment.signal_members(signal.SIGTERM, skip={os.getpid()})
            while containment.members(skip={os.getpid()}):
                if time.monotonic() >= deadline:
                    containment.kill()
                    return _CONTAINMENT_EXIT_CODE
                time.sleep(0.01)
    except ContainmentError as exc:
        print(f"process containment failed: {exc}", file=sys.stderr)
        return _CONTAINMENT_EXIT_CODE
    assert status is not None
    _supervisor_exit(status)
    return _CONTAINMENT_EXIT_CODE


def main(argv: list[str] | None = None) -> int:
    """Run the private supervisor protocol."""
    supplied = sys.argv[1:] if argv is None else argv
    if not supplied or supplied[0] != "--":
        return _CONTAINMENT_EXIT_CODE
    return _run_supervisor(supplied[1:])


if __name__ == "__main__":
    raise SystemExit(main())
