from __future__ import annotations

import os
import runpy
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import atlas_process_supervisor as supervisor
from atlas_process_supervisor import (
    ContainmentError,
    ContainmentUnavailable,
    ProcessContainment,
    spawn_contained,
)


def _python_target(code: str) -> list[str]:
    return [sys.executable, "-c", code]


@pytest.mark.skipif(sys.platform != "linux", reason="cgroup v2 is Linux-only")
def test_post_signal_fork_and_double_setsid_stay_contained(tmp_path: Path) -> None:
    marker = tmp_path / "escaped"
    target = f"""
import os
import signal
import time
from pathlib import Path

def handle(_signum, _frame):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = os.fork()
    if child == 0:
        os.setsid()
        grandchild = os.fork()
        if grandchild == 0:
            time.sleep(20)
            Path({str(marker)!r}).write_text("escaped")
        os._exit(0)
    time.sleep(0.2)

signal.signal(signal.SIGTERM, handle)
while True:
    time.sleep(0.05)
"""
    contained = spawn_contained(_python_target(target))
    started = time.monotonic()
    contained.terminate()
    elapsed = time.monotonic() - started

    assert elapsed < 6.5
    assert contained.process.returncode in {-signal.SIGTERM, -signal.SIGKILL, 125}
    assert not marker.exists()
    contained.cleanup()


def test_exact_argv_streams_and_bytecode_environment(tmp_path: Path) -> None:
    target = (
        "import os, sys; "
        "print('|'.join(sys.argv[1:])); "
        "print(os.environ['PYTHONDONTWRITEBYTECODE'], file=sys.stderr); "
        "raise SystemExit(3)"
    )
    contained = spawn_contained(
        [*_python_target(target), "a", "", "--flag"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = contained.process.communicate(timeout=5)
    contained.cleanup()

    assert contained.process.returncode == 3
    assert stdout == "a||--flag\n"
    assert stderr == "1\n"


def test_containment_unavailable_fails_before_target_start(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "started"
    monkeypatch.setattr(
        "atlas_process_supervisor._current_cgroup",
        lambda: (_ for _ in ()).throw(
            ContainmentUnavailable("delegation unavailable")
        ),
    )

    with pytest.raises(ContainmentUnavailable, match="delegation unavailable"):
        spawn_contained(
            _python_target(f"from pathlib import Path; Path({str(marker)!r}).touch()")
        )
    assert not marker.exists()


def test_containment_path_and_membership_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(ContainmentUnavailable, match="directory is unavailable"):
        ProcessContainment.from_path(missing)

    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    for name in ("cgroup.controllers", "cgroup.procs", "cgroup.kill"):
        (cgroup / name).touch()
    monkeypatch.setattr("atlas_process_supervisor._CGROUP_MOUNT", tmp_path)
    containment = ProcessContainment.from_path(cgroup)
    original_control = __import__("atlas_process_supervisor")._control

    def denied_control(path: Path, name: str) -> Path:
        if name == "cgroup.procs":
            raise PermissionError(f"cannot read {path}/{name}")
        return original_control(path, name)

    monkeypatch.setattr("atlas_process_supervisor._control", denied_control)
    with pytest.raises(ContainmentError, match="cannot read cgroup membership"):
        containment.members()


@pytest.mark.skipif(sys.platform != "linux", reason="parent-death signal is Linux-only")
def test_parent_early_exit_stops_nested_process(tmp_path: Path) -> None:
    marker = tmp_path / "parent-died-child-ran"
    child_code = (
        "import time; from pathlib import Path; "
        f"time.sleep(1); Path({str(marker)!r}).write_text('ran')"
    )
    controller = (
        "import os, sys; "
        "from atlas_process_supervisor import spawn_contained; "
        f"spawn_contained([sys.executable, '-c', {child_code!r}]); "
        "os._exit(0)"
    )
    process = subprocess.run(
        _python_target(controller),
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
        check=False,
        timeout=8,
    )

    assert process.returncode == 0
    time.sleep(1.5)
    assert not marker.exists()


def test_signal_members_reports_permission_error(monkeypatch: pytest.MonkeyPatch) -> None:
    containment = object.__new__(ProcessContainment)
    containment.path = Path("/not-used")
    containment.attached = True
    monkeypatch.setattr(
        "atlas_process_supervisor.ProcessContainment.members",
        lambda self, skip=None: {os.getpid()},
    )
    monkeypatch.setattr(
        "atlas_process_supervisor.os.kill",
        lambda pid, signum: (_ for _ in ()).throw(PermissionError("denied")),
    )
    with pytest.raises(ContainmentError, match="cannot signal cgroup member"):
        containment.signal_members(signal.SIGTERM)


class _FakeContainment:
    def __init__(self, member_results: list[set[int]] | None = None) -> None:
        self.member_results = list(member_results or [set()])
        self.signals: list[int] = []
        self.kills = 0

    def members(self, *, skip=None) -> set[int]:
        if len(self.member_results) > 1:
            return self.member_results.pop(0)
        return self.member_results[0]

    def signal_members(self, signum: int, *, skip=None) -> None:
        self.signals.append(signum)

    def kill(self) -> None:
        self.kills += 1


def _prepare_supervisor_protocol(
    monkeypatch: pytest.MonkeyPatch,
    containment: _FakeContainment,
    handlers: list[object],
    *,
    handshake: bytes = b"1",
) -> None:
    monkeypatch.setenv("ATLAS_SUPERVISOR_READY_FD", "10")
    monkeypatch.setenv("ATLAS_SUPERVISOR_GO_FD", "11")
    monkeypatch.setenv("ATLAS_SUPERVISOR_CGROUP", "/cgroup")
    monkeypatch.setattr(
        supervisor.ProcessContainment,
        "from_path",
        lambda path: containment,
    )
    monkeypatch.setattr(supervisor, "_set_parent_death_signal", lambda: None)
    monkeypatch.setattr(supervisor.os, "write", lambda fd, value: len(value))
    monkeypatch.setattr(supervisor.os, "read", lambda fd, size: handshake)
    monkeypatch.setattr(supervisor.os, "close", lambda fd: None)
    monkeypatch.setattr(
        supervisor.signal,
        "signal",
        lambda signum, handler: handlers.append(handler),
    )
    monkeypatch.setattr(supervisor.signal, "siginterrupt", lambda *args: None)
    monkeypatch.setattr(supervisor.time, "sleep", lambda seconds: None)


def test_supervisor_protocol_waits_for_descendants_before_normal_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    containment = _FakeContainment([{999}, set()])
    handlers: list[object] = []
    _prepare_supervisor_protocol(monkeypatch, containment, handlers)
    exits: list[int] = []
    monkeypatch.setattr(supervisor.os, "fork", lambda: 123)
    monkeypatch.setattr(supervisor.os, "waitpid", lambda pid, options: (123, 0))
    monkeypatch.setattr(supervisor, "_supervisor_exit", exits.append)

    assert supervisor._run_supervisor(["command"]) == 125
    assert exits == [0]
    assert containment.signals == []


def test_supervisor_protocol_stops_on_parent_signal_and_handles_missing_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    containment = _FakeContainment([set(), set()])
    handlers: list[object] = []
    _prepare_supervisor_protocol(monkeypatch, containment, handlers)
    exits: list[int] = []
    waits = iter([(0, 0), (123, 0)])
    monkeypatch.setattr(supervisor.os, "fork", lambda: 123)
    monkeypatch.setattr(supervisor.os, "waitpid", lambda pid, options: next(waits))
    monkeypatch.setattr(supervisor.os, "kill", lambda pid, signum: None)
    monkeypatch.setattr(supervisor, "_supervisor_exit", exits.append)

    original_members = containment.members
    calls = 0

    def request_stop_after_wait(*, skip=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            assert handlers
            handlers[0](signal.SIGTERM, None)
        return original_members(skip=skip)

    containment.members = request_stop_after_wait  # type: ignore[method-assign]
    assert supervisor._run_supervisor(["command"]) == 125
    assert exits == [0]
    assert containment.signals == [signal.SIGTERM]


def test_supervisor_protocol_kills_when_term_deadline_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    containment = _FakeContainment([{999}])
    handlers: list[object] = []
    _prepare_supervisor_protocol(monkeypatch, containment, handlers)
    monkeypatch.setattr(supervisor.os, "fork", lambda: 123)
    monkeypatch.setattr(supervisor.os, "waitpid", lambda pid, options: (0, 0))
    monkeypatch.setattr(supervisor.os, "kill", lambda pid, signum: None)
    monkeypatch.setattr(supervisor.time, "monotonic", iter([0.0, 6.0]).__next__)
    handlers_after_setup = handlers
    original_sleep = supervisor.time.sleep

    def stop_on_sleep(seconds: float) -> None:
        if handlers_after_setup:
            handlers_after_setup[0](signal.SIGTERM, None)
        original_sleep(0)

    monkeypatch.setattr(supervisor.time, "sleep", stop_on_sleep)
    assert supervisor._run_supervisor(["command"]) == 125
    assert containment.kills == 1


def test_supervisor_protocol_kills_residual_members_after_child_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    containment = _FakeContainment([{999}])
    handlers: list[object] = []
    _prepare_supervisor_protocol(monkeypatch, containment, handlers)
    exits: list[int] = []
    monkeypatch.setattr(supervisor.os, "fork", lambda: 123)
    monkeypatch.setattr(supervisor.os, "waitpid", lambda pid, options: (123, 0))
    monkeypatch.setattr(supervisor.os, "kill", lambda pid, signum: (_ for _ in ()).throw(ProcessLookupError))
    monkeypatch.setattr(supervisor, "_supervisor_exit", exits.append)
    monotonic_values = iter([0.0, 6.0])
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: next(monotonic_values))

    def signal_after_child(*, skip=None):
        if not containment.signals:
            handlers[0](signal.SIGTERM, None)
        return {999}

    containment.members = signal_after_child  # type: ignore[method-assign]
    assert supervisor._run_supervisor(["command"]) == 125
    assert containment.kills == 1
    assert exits == []


def test_supervisor_protocol_covers_interrupted_wait_and_term_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    containment = _FakeContainment([set(), set()])
    handlers: list[object] = []
    _prepare_supervisor_protocol(monkeypatch, containment, handlers)
    exits: list[int] = []
    waits = iter([(0, 0), (0, 0), (123, 0)])
    wait_calls = 0

    def waitpid(pid, options):
        nonlocal wait_calls
        wait_calls += 1
        if wait_calls == 1:
            handlers[0](signal.SIGTERM, None)
        return next(waits)

    monkeypatch.setattr(supervisor.os, "fork", lambda: 123)
    monkeypatch.setattr(supervisor.os, "waitpid", waitpid)
    monkeypatch.setattr(supervisor.os, "kill", lambda pid, signum: None)
    monkeypatch.setattr(supervisor.time, "monotonic", iter([0.0, 0.1]).__next__)
    monkeypatch.setattr(supervisor, "_supervisor_exit", exits.append)
    assert supervisor._run_supervisor(["command"]) == 125
    assert exits == [0]

    containment = _FakeContainment([{999}, set()])
    handlers = []
    _prepare_supervisor_protocol(monkeypatch, containment, handlers)
    exits = []
    monkeypatch.setattr(supervisor.os, "fork", lambda: 123)
    monkeypatch.setattr(supervisor.os, "waitpid", lambda pid, options: (123, 0))
    monkeypatch.setattr(supervisor.os, "kill", lambda pid, signum: None)
    monkeypatch.setattr(supervisor, "_supervisor_exit", exits.append)
    member_calls = 0

    def signal_after_exit(*, skip=None):
        nonlocal member_calls
        member_calls += 1
        if member_calls == 2:
            handlers[0](signal.SIGTERM, None)
        return {999} if member_calls in {1, 2, 3} else set()

    containment.members = signal_after_exit  # type: ignore[method-assign]
    monkeypatch.setattr(supervisor.time, "monotonic", iter([0.0, 0.1]).__next__)
    assert supervisor._run_supervisor(["command"]) == 125
    assert exits == [0]
    assert containment.signals == [signal.SIGTERM]


def test_supervisor_protocol_handles_wait_interruption_and_child_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    containment = _FakeContainment([set()])
    handlers: list[object] = []
    _prepare_supervisor_protocol(monkeypatch, containment, handlers)
    exits: list[int] = []
    waits = iter([InterruptedError(), (123, 0)])

    def waitpid(pid, options):
        result = next(waits)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(supervisor.os, "fork", lambda: 123)
    monkeypatch.setattr(supervisor.os, "waitpid", waitpid)
    monkeypatch.setattr(supervisor, "_supervisor_exit", exits.append)
    assert supervisor._run_supervisor(["command"]) == 125
    assert exits == [0]

    containment = _FakeContainment([set()])
    handlers = []
    _prepare_supervisor_protocol(monkeypatch, containment, handlers)
    monkeypatch.setattr(supervisor.os, "fork", lambda: 123)
    monkeypatch.setattr(supervisor.os, "waitpid", lambda pid, options: (123, 0))
    monkeypatch.setattr(supervisor.os, "write", lambda fd, value: len(value))
    def stop_before_fork(fd, size):
        handlers[0](signal.SIGTERM, None)
        return b"1"

    monkeypatch.setattr(supervisor.os, "read", stop_before_fork)
    monkeypatch.setattr(supervisor, "_supervisor_exit", lambda status: None)
    assert supervisor._run_supervisor(["command"]) == 125


def test_supervisor_protocol_rejects_missing_handshake_and_fork_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    containment = _FakeContainment()
    handlers: list[object] = []
    _prepare_supervisor_protocol(monkeypatch, containment, handlers, handshake=b"0")
    monkeypatch.setattr(supervisor.os, "fork", lambda: 123)
    assert supervisor._run_supervisor(["command"]) == 125

    handlers.clear()
    _prepare_supervisor_protocol(monkeypatch, containment, handlers)
    monkeypatch.setattr(
        supervisor.os,
        "write",
        lambda fd, value: (_ for _ in ()).throw(OSError("pipe closed")),
    )
    assert supervisor._run_supervisor(["command"]) == 125

    handlers.clear()
    _prepare_supervisor_protocol(monkeypatch, containment, handlers)
    monkeypatch.setattr(
        supervisor.ProcessContainment,
        "from_path",
        lambda path: (_ for _ in ()).throw(ContainmentError("cgroup gone")),
    )
    assert supervisor._run_supervisor(["command"]) == 125

    handlers.clear()
    _prepare_supervisor_protocol(monkeypatch, containment, handlers)
    monkeypatch.setattr(
        supervisor.os,
        "fork",
        lambda: (_ for _ in ()).throw(OSError("fork failed")),
    )
    assert supervisor._run_supervisor(["command"]) == 125


def test_supervisor_protocol_fails_closed_on_containment_permission_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    containment = _FakeContainment([{999}])
    handlers: list[object] = []
    _prepare_supervisor_protocol(monkeypatch, containment, handlers)
    monkeypatch.setattr(supervisor.os, "fork", lambda: 123)
    monkeypatch.setattr(supervisor.os, "waitpid", lambda pid, options: (0, 0))
    monkeypatch.setattr(supervisor.os, "kill", lambda pid, signum: None)
    monkeypatch.setattr(supervisor.time, "monotonic", iter([0.0, 6.0]).__next__)

    def denied_signal(signum: int, *, skip=None) -> None:
        raise ContainmentError("cannot signal cgroup")

    containment.signal_members = denied_signal
    handlers_after_setup = handlers

    def stop_on_sleep(seconds: float) -> None:
        handlers_after_setup[0](signal.SIGTERM, None)

    monkeypatch.setattr(supervisor.time, "sleep", stop_on_sleep)
    assert supervisor._run_supervisor(["command"]) == 125


def test_supervisor_protocol_fails_closed_when_child_signal_is_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    containment = _FakeContainment([set()])
    handlers: list[object] = []
    _prepare_supervisor_protocol(monkeypatch, containment, handlers)
    monkeypatch.setattr(supervisor.os, "fork", lambda: 123)
    monkeypatch.setattr(
        supervisor.os,
        "kill",
        lambda pid, signum: (_ for _ in ()).throw(PermissionError("denied")),
    )

    def waitpid(pid, options):
        handlers[0](signal.SIGTERM, None)
        return (0, 0)

    monkeypatch.setattr(supervisor.os, "waitpid", waitpid)
    assert supervisor._run_supervisor(["command"]) == 125


def test_supervisor_protocol_child_exec_errors_are_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExitProbe(Exception):
        def __init__(self, code: int):
            self.code = code

    for error, code in [(FileNotFoundError(), 127), (OSError("denied"), 126)]:
        containment = _FakeContainment()
        handlers: list[object] = []
        _prepare_supervisor_protocol(monkeypatch, containment, handlers)
        monkeypatch.setattr(supervisor.os, "fork", lambda: 0)
        monkeypatch.setattr(
            supervisor.os,
            "execvpe",
            lambda *args, error=error: (_ for _ in ()).throw(error),
        )
        monkeypatch.setattr(
            supervisor.os,
            "_exit",
            lambda exit_code: (_ for _ in ()).throw(ExitProbe(exit_code)),
        )
        with pytest.raises(ExitProbe) as raised:
            supervisor._run_supervisor(["command"])
        assert raised.value.code == code


def test_supervisor_helpers_fail_closed_and_cover_protocol_entrypoints(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("ATLAS_SUPERVISOR_READY_FD", raising=False)
    monkeypatch.delenv("ATLAS_SUPERVISOR_GO_FD", raising=False)
    monkeypatch.delenv("ATLAS_SUPERVISOR_CGROUP", raising=False)
    assert supervisor._run_supervisor(["command"]) == 125
    assert supervisor.main([]) == 125
    assert supervisor.main(["not-a-protocol"]) == 125
    monkeypatch.setattr(supervisor.sys, "argv", ["supervisor", "--", "command"])
    monkeypatch.setattr(supervisor, "_run_supervisor", lambda argv: len(argv))
    assert supervisor.main() == 1

    control = tmp_path / "cgroup"
    control.mkdir()
    with pytest.raises(ContainmentUnavailable, match="control is unavailable"):
        supervisor._control(control, "cgroup.procs")
    (control / "cgroup.procs").write_text("\n1\n", encoding="ascii")
    assert supervisor._read_pids(control) == {1}
    (control / "cgroup.procs").write_text("invalid\n", encoding="ascii")
    with pytest.raises(ContainmentError, match="invalid cgroup membership"):
        supervisor._read_pids(control)

    with pytest.raises(SystemExit) as raised:
        runpy.run_path(str(Path(supervisor.__file__)), run_name="__main__")
    assert raised.value.code == 125


def test_supervisor_cgroup_validation_and_creation_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with pytest.raises(ContainmentUnavailable, match="requires Linux"):
        monkeypatch.setattr(supervisor.sys, "platform", "darwin")
        supervisor.ProcessContainment.create()
    monkeypatch.setattr(supervisor.sys, "platform", "linux")

    parent = tmp_path / "parent"
    parent.mkdir()
    original_mkdir = supervisor.Path.mkdir
    monkeypatch.setattr(supervisor, "_current_cgroup", lambda: parent)
    monkeypatch.setattr(
        supervisor.Path,
        "mkdir",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("mkdir failed")),
    )
    with pytest.raises(ContainmentUnavailable, match="cannot create delegated cgroup"):
        supervisor.ProcessContainment.create()

    parent = tmp_path / "parent-two"
    original_mkdir(parent)
    monkeypatch.setattr(supervisor, "_current_cgroup", lambda: parent)
    monkeypatch.setattr(supervisor.Path, "mkdir", original_mkdir)
    monkeypatch.setattr(
        supervisor,
        "_validated_cgroup",
        lambda path: (_ for _ in ()).throw(ContainmentUnavailable("invalid child")),
    )
    with pytest.raises(ContainmentUnavailable, match="invalid child"):
        supervisor.ProcessContainment.create()

    parent = tmp_path / "parent-three"
    original_mkdir(parent)
    monkeypatch.setattr(supervisor, "_current_cgroup", lambda: parent)
    monkeypatch.setattr(
        supervisor,
        "_validated_cgroup",
        lambda path: (_ for _ in ()).throw(ContainmentUnavailable("invalid child")),
    )
    monkeypatch.setattr(
        supervisor.Path,
        "rmdir",
        lambda path: (_ for _ in ()).throw(OSError("cannot remove child")),
    )
    with pytest.raises(ContainmentUnavailable, match="invalid child"):
        supervisor.ProcessContainment.create()


def test_supervisor_current_cgroup_and_parent_death_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_read = supervisor.Path.read_text
    monkeypatch.setattr(
        supervisor.Path,
        "read_text",
        lambda path, **kwargs: (_ for _ in ()).throw(OSError("unreadable")),
    )
    with pytest.raises(ContainmentUnavailable, match="cannot be read"):
        supervisor._current_cgroup()
    monkeypatch.setattr(
        supervisor.Path,
        "read_text",
        lambda path, **kwargs: "1:name:/group\n",
    )
    with pytest.raises(ContainmentUnavailable, match="v2"):
        supervisor._current_cgroup()
    monkeypatch.setattr(
        supervisor.Path,
        "read_text",
        lambda path, **kwargs: "0::/../unsafe\n",
    )
    with pytest.raises(ContainmentUnavailable, match="unsafe"):
        supervisor._current_cgroup()
    monkeypatch.setattr(supervisor.Path, "read_text", original_read)

    class Libc:
        def prctl(self, *args):
            return 1

    monkeypatch.setattr(supervisor.ctypes, "CDLL", lambda *args, **kwargs: Libc())
    monkeypatch.setattr(supervisor.ctypes, "get_errno", lambda: 13)
    with pytest.raises(ContainmentError, match="errno 13"):
        supervisor._set_parent_death_signal()

    monkeypatch.setattr(Libc, "prctl", lambda self, *args: 0)
    monkeypatch.setattr(supervisor.os, "getppid", iter([10, 11]).__next__)
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(supervisor.os, "kill", lambda pid, signum: killed.append((pid, signum)))
    monkeypatch.setattr(supervisor.os, "getpid", lambda: 10)
    supervisor._set_parent_death_signal()
    assert killed == [(10, signal.SIGTERM)]


def test_supervisor_parent_death_signal_without_pid_change_and_exit_forwarding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Libc:
        def prctl(self, *args):
            return 0

    monkeypatch.setattr(supervisor.ctypes, "CDLL", lambda *args, **kwargs: Libc())
    monkeypatch.setattr(supervisor.os, "getppid", lambda: 10)
    monkeypatch.setattr(supervisor.os, "getpid", lambda: 10)
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(supervisor.os, "kill", lambda pid, signum: killed.append((pid, signum)))
    supervisor._set_parent_death_signal()
    assert killed == []

    class ExitProbe(Exception):
        def __init__(self, code: int):
            self.code = code

    monkeypatch.setattr(
        supervisor.os,
        "_exit",
        lambda code: (_ for _ in ()).throw(ExitProbe(code)),
    )
    with pytest.raises(ExitProbe) as exited:
        supervisor._supervisor_exit(3 << 8)
    assert exited.value.code == 3

    monkeypatch.setattr(supervisor.signal, "signal", lambda *args: None)
    monkeypatch.setattr(
        supervisor.os,
        "kill",
        lambda pid, signum: (_ for _ in ()).throw(ExitProbe(128 + signum)),
    )
    with pytest.raises(ExitProbe) as signaled:
        supervisor._supervisor_exit(signal.SIGTERM)
    assert signaled.value.code == 128 + signal.SIGTERM

    with pytest.raises(ExitProbe) as unknown:
        supervisor._supervisor_exit(255)
    assert unknown.value.code == 125


def test_supervisor_cgroup_controls_attach_signal_and_kill(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    monkeypatch.setattr(supervisor, "_CGROUP_MOUNT", tmp_path)
    (cgroup / "cgroup.controllers").write_text("pids\n", encoding="ascii")
    (cgroup / "cgroup.procs").write_text("7\n", encoding="ascii")
    (cgroup / "cgroup.kill").write_text("0\n", encoding="ascii")
    containment = ProcessContainment.from_path(cgroup)
    containment.attach(7)
    with pytest.raises(ContainmentError, match="already attached"):
        containment.attach(7)

    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        supervisor.os,
        "kill",
        lambda pid, signum: (_ for _ in ()).throw(ProcessLookupError)
        if pid == 7
        else signals.append((pid, signum)),
    )
    containment.signal_members(signal.SIGTERM, skip={999})
    assert signals == []
    containment.kill()
    assert (cgroup / "cgroup.kill").read_text(encoding="ascii") == "1\n"

    monkeypatch.setattr(
        supervisor.os,
        "open",
        lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("denied")),
    )
    with pytest.raises(ContainmentUnavailable, match="cannot write cgroup control"):
        supervisor._write_control(cgroup, "cgroup.kill", "1")

    monkeypatch.setattr(supervisor.os, "open", lambda *args, **kwargs: -1)
    monkeypatch.setattr(
        supervisor.os,
        "write",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("write failed")),
    )
    with pytest.raises(ContainmentUnavailable, match="cannot write cgroup control"):
        supervisor._write_control(cgroup, "cgroup.kill", "1")

    monkeypatch.setattr(supervisor.os, "write", lambda *args, **kwargs: None)
    supervisor._write_control(cgroup, "cgroup.kill", "1")

    not_retained = object.__new__(ProcessContainment)
    not_retained.path = cgroup
    not_retained.attached = False
    not_retained.members = lambda skip=None: set()  # type: ignore[method-assign]
    monkeypatch.setattr(supervisor, "_write_control", lambda *args, **kwargs: None)
    with pytest.raises(ContainmentError, match="not retained"):
        not_retained.attach(8)


def test_supervisor_termination_and_cleanup_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "cgroup"
    path.mkdir()
    containment = object.__new__(ProcessContainment)
    containment.path = path
    containment.attached = True

    class Finished:
        def poll(self):
            return 0

    containment.members = lambda skip=None: set()  # type: ignore[method-assign]
    containment.signal_members = lambda signum, skip=None: None  # type: ignore[method-assign]
    containment.terminate(Finished())
    with pytest.raises(ContainmentError, match="already attempted"):
        containment.terminate(Finished())

    class Stubborn:
        def poll(self):
            return None

        def wait(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired("command", timeout)
            return None

    calls = 0

    def members(*, skip=None):
        nonlocal calls
        calls += 1
        return {9} if calls == 1 else set()

    containment.members = members  # type: ignore[method-assign]
    containment.termination_started = True
    with pytest.raises(ContainmentError, match="after termination attempt"):
        containment.cleanup(Stubborn())
    killed: list[bool] = []
    containment.kill = lambda: killed.append(True)  # type: ignore[method-assign]
    monkeypatch.setattr(supervisor.time, "monotonic", iter([0.0, 0.1, 6.0]).__next__)
    containment.termination_started = False
    containment.terminate(Stubborn())
    assert killed == [True]

    containment.members = lambda skip=None: {9}  # type: ignore[method-assign]
    monkeypatch.setattr(supervisor.time, "monotonic", iter([0.0, 6.0]).__next__)
    containment.termination_started = False
    with pytest.raises(ContainmentError, match="still has members"):
        containment.terminate(Stubborn())

    class WaitFailure(Stubborn):
        def wait(self, timeout=None):
            if timeout is None:
                raise OSError("wait failed")
            return super().wait(timeout=timeout)

    containment.members = lambda skip=None: {9}  # type: ignore[method-assign]
    monkeypatch.setattr(supervisor.time, "monotonic", iter([0.0, 6.0]).__next__)
    containment.termination_started = False
    with pytest.raises(ContainmentError, match="did not terminate"):
        containment.terminate(WaitFailure())

    containment.members = lambda skip=None: {9}  # type: ignore[method-assign]
    containment.terminate = lambda process: None  # type: ignore[method-assign]
    containment.termination_started = False
    containment.cleanup(Stubborn())
    monkeypatch.setattr(
        supervisor.Path,
        "rmdir",
        lambda path: (_ for _ in ()).throw(OSError("rmdir failed")),
    )
    with pytest.raises(ContainmentError, match="cannot remove execution cgroup"):
        containment.cleanup(Stubborn())


def test_supervisor_abort_launch_and_handshake_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Process:
        def __init__(self, running: bool = True):
            self.running = running
            self.killed = False

        def poll(self):
            return None if self.running else 0

        def kill(self):
            self.killed = True

        def wait(self):
            self.running = False

    path = tmp_path / "cgroup"
    path.mkdir()
    containment = object.__new__(ProcessContainment)
    containment.path = path
    containment.attached = False
    containment.members = lambda skip=None: {1}  # type: ignore[method-assign]
    containment.kill = lambda: None  # type: ignore[method-assign]
    process = Process()
    supervisor._abort_launch(process, containment)
    assert process.killed
    assert not path.exists()

    class BrokenProcess(Process):
        def wait(self):
            raise OSError("wait failed")

    path = tmp_path / "broken-cgroup"
    path.mkdir()
    containment.path = path
    containment.members = lambda skip=None: (_ for _ in ()).throw(
        ContainmentError("membership failed")
    )  # type: ignore[method-assign]
    supervisor._abort_launch(BrokenProcess(), containment)
    assert not path.exists()

    path = tmp_path / "attached-cgroup"
    path.mkdir()
    containment.path = path
    containment.attached = True
    containment.members = lambda skip=None: set()  # type: ignore[method-assign]
    supervisor._abort_launch(Process(), containment)
    assert not path.exists()

    path = tmp_path / "rmdir-cgroup"
    path.mkdir()
    containment.path = path
    containment.attached = False
    containment.members = lambda skip=None: set()  # type: ignore[method-assign]
    monkeypatch.setattr(
        supervisor.Path,
        "rmdir",
        lambda path: (_ for _ in ()).throw(OSError("rmdir failed")),
    )
    supervisor._abort_launch(Process(running=False), containment)

    class ReadyProcess:
        pid = 7

        def poll(self):
            return None

    process = ReadyProcess()
    monkeypatch.setattr(supervisor.time, "monotonic", iter([0.0, 2.0]).__next__)
    monkeypatch.setattr(supervisor.select, "select", lambda *args: ([], [], []))
    with pytest.raises(ContainmentError, match="handshake timed out"):
        supervisor._read_ready(1, process, timeout_seconds=1)

    monkeypatch.setattr(supervisor.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(supervisor.select, "select", lambda *args: ([], [], []))
    monkeypatch.setattr(process, "poll", lambda: 1)
    with pytest.raises(ContainmentError, match="exited before attachment"):
        supervisor._read_ready(1, process, timeout_seconds=1)

    monkeypatch.setattr(supervisor.select, "select", lambda *args: ([1], [], []))
    monkeypatch.setattr(supervisor.os, "read", lambda fd, size: b"")
    with pytest.raises(ContainmentError, match="closed its handshake"):
        supervisor._read_ready(1, process, timeout_seconds=1)

    chunks = iter([b"7", b"\n"])
    monkeypatch.setattr(supervisor.os, "read", lambda fd, size: next(chunks))
    supervisor._read_ready(1, process, timeout_seconds=1)

    monkeypatch.setattr(supervisor.os, "read", lambda fd, size: b"abc\n")
    with pytest.raises(ContainmentError, match="invalid handshake"):
        supervisor._read_ready(1, process, timeout_seconds=1)

    monkeypatch.setattr(supervisor.os, "read", lambda fd, size: b"8\n")
    with pytest.raises(ContainmentError, match="PID mismatch"):
        supervisor._read_ready(1, process, timeout_seconds=1)

    waiting = ReadyProcess()
    select_results = iter([([], [], []), ([1], [], [])])
    monkeypatch.setattr(
        supervisor.select,
        "select",
        lambda *args: next(select_results),
    )
    monkeypatch.setattr(supervisor.time, "monotonic", iter([0.0, 0.0, 0.0]).__next__)
    monkeypatch.setattr(supervisor.os, "read", lambda fd, size: b"7\n")
    supervisor._read_ready(1, waiting, timeout_seconds=1)


def test_supervisor_spawn_preconditions_and_cgroup_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        spawn_contained([])
    monkeypatch.setattr(supervisor.sys, "platform", "darwin")
    with pytest.raises(ContainmentUnavailable, match="requires Linux"):
        spawn_contained(["command"])
    monkeypatch.setattr(supervisor.sys, "platform", "linux")
    with pytest.raises(ContainmentUnavailable, match="supervisor is unavailable"):
        spawn_contained(["command"], supervisor_path=tmp_path / "missing")

    mount = tmp_path / "mount"
    mount.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(supervisor, "_CGROUP_MOUNT", mount)
    with pytest.raises(ContainmentUnavailable, match="outside the cgroup mount"):
        supervisor._validated_cgroup(outside)
    link = tmp_path / "link"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ContainmentUnavailable, match="directory is unavailable"):
        supervisor._validated_cgroup(link)
