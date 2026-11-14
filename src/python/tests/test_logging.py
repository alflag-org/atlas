"""Application logging contracts, including real filesystem and rotation behavior."""

from __future__ import annotations

import errno
import io
import json
import logging
import os
import re
import stat
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock

import pytest

from atlas_core import logging as implementation
from atlas_core.logging import configure_logging, get_logger

pytestmark = pytest.mark.usefixtures("application_logging")


def test_default_output_is_lazy_utf8_utc_and_shared(tmp_path, capsys):
    logger = get_logger("hermes.dns")
    assert isinstance(logger, logging.Logger)
    assert logger is logging.getLogger("hermes.dns")
    assert logger.handlers == []
    path = tmp_path / "hermes.log"
    assert not path.exists()
    logger.debug("hidden")
    assert not path.exists()
    logger.info("検証 completed")
    output = capsys.readouterr()
    assert output.out == ""
    assert path.read_text(encoding="utf-8") == output.err
    assert re.fullmatch(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z INFO hermes.dns 検証 completed\n",
        output.err,
    )
    assert get_logger("hermes.proxmox").parent is get_logger("hermes")


def test_configuration_preserves_unspecified_values_and_user_handlers(tmp_path, capsys):
    root = logging.getLogger()
    root_state = root.level, root.propagate, list(root.handlers)
    other = logging.getLogger("unrelated")
    other_state = other.level, other.propagate, list(other.handlers)
    logger = logging.getLogger("hermes")
    sink = io.StringIO()
    user_handler = logging.StreamHandler(sink)
    logger.addHandler(user_handler)
    first_path, second_path = tmp_path / "first.log", tmp_path / "second.log"
    configure_logging("hermes", level="DEBUG", path=first_path)
    child = get_logger("hermes.worker")
    child.debug("first")
    first_handler = implementation._FILES["hermes"]
    descriptor = first_handler.descriptor
    assert configure_logging("hermes", path=second_path) is logger
    with pytest.raises(OSError):
        os.fstat(descriptor)
    configure_logging("hermes", path=second_path)
    get_logger("hermes.worker").debug("second")
    configure_logging("hermes", level="ERROR")
    assert implementation._FILES["hermes"].path == second_path
    get_logger("hermes").error("third")
    assert logger.level == logging.ERROR
    assert len(logger.handlers) == 3
    assert user_handler in logger.handlers
    assert sink.getvalue() == "first\nsecond\nthird\n"
    assert first_path.read_text().endswith("first\n")
    assert len(second_path.read_text().splitlines()) == 2
    assert capsys.readouterr().err.count("second") == 1
    assert (root.level, root.propagate, root.handlers) == root_state
    assert (other.level, other.propagate, other.handlers) == other_state


def test_levels_inherit_and_standard_mutation_survives_retrieval(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setenv("ATLAS_LOG_LEVEL", "warning")
    child = get_logger("hermes.worker")
    parent = get_logger("hermes")
    assert child.level == logging.NOTSET
    assert not child.isEnabledFor(logging.INFO)
    monkeypatch.setenv("ATLAS_LOG_LEVEL", "CRITICAL")
    parent.setLevel(logging.DEBUG)
    assert get_logger("hermes").level == logging.DEBUG
    child.debug("inherited")
    parent.setLevel(logging.ERROR)
    child.setLevel(logging.DEBUG)
    assert get_logger("hermes.worker").level == logging.DEBUG
    child.debug("child override")
    assert all(handler.level == logging.NOTSET for handler in parent.handlers)
    assert len((tmp_path / "hermes.log").read_text().splitlines()) == 2
    assert capsys.readouterr().err.count("DEBUG") == 2


def test_environment_level_warning_only_at_initialization_and_explicit_precedence(
    monkeypatch, capsys
):
    monkeypatch.setenv("ATLAS_LOG_LEVEL", "secret-invalid-level")
    assert get_logger("hermes").level == logging.INFO
    get_logger("hermes.worker")
    configure_logging("hermes")
    assert configure_logging("ares", level=logging.DEBUG).level == logging.DEBUG
    output = capsys.readouterr().err
    assert output.count("invalid ATLAS_LOG_LEVEL") == 1
    assert "secret-invalid-level" not in output


@pytest.mark.parametrize("level", ["NOTSET", 0, 15, True, [], "verbose"])
def test_invalid_level_does_not_partially_reconfigure(level, tmp_path):
    logger = configure_logging("hermes", path=tmp_path / "original")
    with pytest.raises(ValueError):
        configure_logging("hermes", level=level, path=tmp_path / "new")
    assert logger.level == logging.INFO
    assert implementation._FILES["hermes"].path == tmp_path / "original"


@pytest.mark.parametrize(
    "name", ["", "root", "root.child", "../hermes", "hermes..dns", "hermes\n"]
)
def test_invalid_names(name):
    with pytest.raises(ValueError):
        get_logger(name)


def test_invalid_name_type_and_child_configuration():
    with pytest.raises(TypeError):
        get_logger(None)
    with pytest.raises(ValueError, match="top-level"):
        configure_logging("hermes.dns")


@pytest.mark.parametrize(
    "path",
    [
        "relative.log",
        "/tmp/../escape",
        "/tmp/line\nlog",
        "/tmp/null\0log",
        "/tmp/del\x7flog",
    ],
)
def test_invalid_explicit_paths(path):
    with pytest.raises(ValueError):
        configure_logging("hermes", path=path)


@pytest.mark.parametrize("program", ["../escape", "bad\\name", "bad\nname", "a..b"])
def test_invalid_program_is_rejected_unless_path_is_explicit(
    program, monkeypatch, tmp_path
):
    monkeypatch.setenv("ATLAS_PROGRAM_NAME", program)
    with pytest.raises(ValueError, match="program name"):
        get_logger("hermes")
    configure_logging("hermes", path=tmp_path / "explicit.log").info("safe")
    assert (tmp_path / "explicit.log").exists()


def test_program_path_overrides_namespace(monkeypatch, tmp_path):
    monkeypatch.setenv("ATLAS_PROGRAM_NAME", "provisioning")
    get_logger("hermes.dns").info("dns")
    get_logger("ares.compute").info("compute")
    lines = (tmp_path / "provisioning.log").read_text().splitlines()
    assert "hermes.dns dns" in lines[0]
    assert "ares.compute compute" in lines[1]
    assert not (tmp_path / "hermes.log").exists()


def test_context_file_supplies_host_run_and_program_without_host_config(
    monkeypatch, tmp_path, capsys
):
    context_file = tmp_path / "context.json"
    context_file.write_text(
        json.dumps(
            {
                "version": 1,
                "host": {"id": "control01"},
                "program": {
                    "name": "provisioning",
                    "root": "/srv/provisioning",
                    "runtime": {"type": "python"},
                },
                "command": {
                    "name": "check",
                    "path": "/srv/provisioning/commands/check.py",
                    "type": "python",
                },
                "execution": {
                    "run_id": "run-1",
                    "parent_run_id": None,
                    "operation_id": "op-1",
                },
                "working_directory": "/tmp",
            }
        )
    )
    monkeypatch.setenv("ATLAS_CONTEXT_FILE", str(context_file))
    monkeypatch.setenv("ATLAS_HOST_FILE", str(tmp_path / "missing-host.yml"))
    get_logger("hermes").info("done")
    content = (tmp_path / "provisioning.log").read_text()
    assert "hermes host_id=control01 run_id=run-1 operation_id=op-1 done" in content
    assert capsys.readouterr().err == content


@pytest.mark.parametrize("context", [None, "missing", "malformed"])
def test_optional_context_falls_back_to_available_environment(
    context, monkeypatch, tmp_path
):
    if context is not None:
        path = tmp_path / "context.json"
        if context == "malformed":
            path.write_text("not json")
        monkeypatch.setenv("ATLAS_CONTEXT_FILE", str(path))
    monkeypatch.setenv("ATLAS_HOST_ID", "control01\nforged\r\t")
    monkeypatch.setenv("ATLAS_RUN_ID", "run-2")
    monkeypatch.setenv("ATLAS_OPERATION_ID", "op-2")
    get_logger("hermes").info("done")
    content = (tmp_path / "hermes.log").read_text()
    assert len(content.splitlines()) == 1
    assert "host_id=control01\\nforged\\r\\t run_id=run-2 operation_id=op-2" in content


def test_exception_and_stack_and_user_record_are_preserved(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_HOST_ID", "control01")
    logger = get_logger("hermes")
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logger.addHandler(handler)
    try:
        raise ValueError("safe detail")
    except ValueError:
        logger.exception("failed %s", "request", stack_info=True)
    content = (tmp_path / "hermes.log").read_text()
    assert "ValueError: safe detail" in content
    assert "Stack (most recent call last)" in content
    assert "failed request" in content
    assert records[0].msg == "failed %s"
    assert records[0].args == ("request",)


@pytest.mark.parametrize(("mask", "expected"), [(0, 0o640), (0o077, 0o600)])
def test_file_creation_respects_umask(mask, expected, tmp_path):
    path = tmp_path / "new.log"
    logger = configure_logging("hermes", path=path)
    previous = os.umask(mask)
    try:
        logger.info("created")
    finally:
        os.umask(previous)
    assert stat.S_IMODE(path.stat().st_mode) == expected


def test_existing_file_is_appended_without_chmod(tmp_path):
    path = tmp_path / "existing.log"
    path.write_text("existing\n")
    path.chmod(0o604)
    before = path.stat()
    configure_logging("hermes", path=path).info("next")
    assert path.read_text().startswith("existing\n")
    after = path.stat()
    assert (after.st_uid, after.st_gid, after.st_mode) == (
        before.st_uid,
        before.st_gid,
        before.st_mode,
    )


def test_parallel_initialization_has_one_pair_of_handlers(tmp_path, capsys):
    def emit(index):
        configure_logging("hermes", path=tmp_path / "parallel.log")
        get_logger("hermes.worker").info("record-%s", index)

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(emit, range(60)))
    assert len(get_logger("hermes").handlers) == 2
    assert len((tmp_path / "parallel.log").read_text().splitlines()) == 60
    assert len(capsys.readouterr().err.splitlines()) == 60


def test_missing_directory_recovery_and_new_outage_warn_once_per_file(tmp_path, capsys):
    directory = tmp_path / "missing"
    path = directory / "app.log"
    first = configure_logging("hermes", path=path)
    second = configure_logging("ares", path=path)
    first.info("lost-one")
    second.info("lost-two")
    assert not directory.exists()
    output = capsys.readouterr().err
    assert output.count("file output unavailable") == 1
    assert "lost-one" in output and "lost-two" in output
    directory.mkdir()
    first.info("recovered")
    assert path.read_text().endswith("recovered\n")
    assert "lost-" not in path.read_text()
    path.unlink()
    directory.rmdir()
    second.info("lost-three")
    first.info("lost-four")
    assert capsys.readouterr().err.count("file output unavailable") == 1


@pytest.mark.parametrize("failure", ["open", "write", "stat", "zero-write"])
def test_io_failure_does_not_escape_and_recovers(
    failure, tmp_path, monkeypatch, capsys
):
    path = tmp_path / "app.log"
    logger = configure_logging("hermes", path=path)
    with monkeypatch.context() as patch:
        method = "write" if failure == "zero-write" else failure
        if failure == "zero-write":
            patch.setattr(implementation.os, method, Mock(return_value=0))
        else:
            patch.setattr(
                implementation.os,
                method,
                Mock(side_effect=OSError(errno.ENOSPC, "private-error-body")),
            )
        logger.info("during failure")
        logger.info("still failing")
    output = capsys.readouterr().err
    assert output.count("file output unavailable") == 1
    assert "private-error-body" not in output
    assert "during failure" in output and "still failing" in output
    logger.info("recovered")
    assert path.read_text().endswith("recovered\n")
    assert "during failure" not in path.read_text()


def test_short_write_completes_record_without_replay(tmp_path, monkeypatch):
    original = os.write
    monkeypatch.setattr(
        implementation.os, "write", lambda fd, data: original(fd, data[:3])
    )
    configure_logging("hermes", path=tmp_path / "app.log").info("検証 completed")
    assert (tmp_path / "app.log").read_text().endswith("検証 completed\n")


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses DAC write permissions")
def test_real_permission_failure_and_recovery(tmp_path, capsys):
    path = tmp_path / "app.log"
    path.touch(mode=0o400)
    logger = configure_logging("hermes", path=path)
    logger.info("denied")
    assert "Permission denied" in capsys.readouterr().err
    assert path.read_text() == ""
    path.chmod(0o600)
    logger.info("recovered")
    assert path.read_text().endswith("recovered\n")


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses DAC write permissions")
def test_rotation_rename_create_and_failed_reopen(tmp_path, capsys):
    path = tmp_path / "app.log"
    old = tmp_path / "app.log.1"
    logger = configure_logging("hermes", path=path)
    logger.info("before")
    path.rename(old)
    path.touch(mode=0o400)
    logger.info("denied")
    logger.info("denied again")
    assert capsys.readouterr().err.count("file output unavailable") == 1
    path.chmod(0o600)
    logger.info("after")
    assert old.read_text().endswith("before\n")
    assert path.read_text().endswith("after\n")
    assert len(path.read_text().splitlines()) == 1
    path.rename(tmp_path / "app.log.2")
    logger.info("create after rename")
    assert path.read_text().endswith("create after rename\n")


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory", "parent-symlink"])
@pytest.mark.parametrize("rotated", [False, True])
def test_unsafe_paths_are_rejected_on_open_and_rotation(
    kind, rotated, tmp_path, capsys
):
    parent = tmp_path / "logs"
    parent.mkdir()
    path = parent / "app.log"
    target = tmp_path / "target"
    target.write_text("untouched")
    logger = configure_logging("hermes", path=path)
    if rotated:
        logger.info("before")
        path.rename(parent / "old.log")
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    else:
        parent.rename(tmp_path / "original")
        parent.symlink_to(tmp_path / "original", target_is_directory=True)
    logger.info("refused")
    assert "file output unavailable" in capsys.readouterr().err
    assert target.read_text() == "untouched"
    if rotated:
        old = (
            tmp_path / "original" if kind == "parent-symlink" else parent
        ) / "old.log"
        assert "refused" not in old.read_text()


def test_fifo_replacement_between_stat_and_open_is_rejected(
    tmp_path, monkeypatch, capsys
):
    real_open = os.open
    path = tmp_path / "app.log"
    descriptors = []

    def raced_open(name, flags, mode=0o777, *, dir_fd=None):
        if name == path.name:
            os.mkfifo(path)
            # A reader lets O_WRONLY | O_NONBLOCK open the FIFO; fstat must reject it.
            descriptors.append(real_open(path, os.O_RDONLY | os.O_NONBLOCK))
            descriptor = real_open(name, flags, mode, dir_fd=dir_fd)
            descriptors.append(descriptor)
            return descriptor
        return real_open(name, flags, mode, dir_fd=dir_fd)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(implementation.os, "open", raced_open)
            configure_logging("hermes", path=path).info("refused")
        assert "file output unavailable" in capsys.readouterr().err
        with pytest.raises(OSError):
            os.fstat(descriptors[1])
        assert os.read(descriptors[0], 4096) == b""
    finally:
        os.close(descriptors[0])


def test_directory_close_failure_releases_next_descriptor(
    tmp_path, monkeypatch, capsys
):
    real_open, real_close = os.open, os.close
    descriptors = []
    failed = False

    def tracked_open(*args, **kwargs):
        descriptor = real_open(*args, **kwargs)
        descriptors.append(descriptor)
        return descriptor

    def failed_close(descriptor):
        nonlocal failed
        real_close(descriptor)
        if not failed:
            failed = True
            raise OSError(errno.EIO, "close failed")

    logger = get_logger("hermes")
    with monkeypatch.context() as patch:
        patch.setattr(implementation.os, "open", tracked_open)
        patch.setattr(implementation.os, "close", failed_close)
        logger.info("during close failure")
    assert "file output unavailable" in capsys.readouterr().err
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    logger.info("recovered")
    assert (tmp_path / "hermes.log").read_text().endswith("recovered\n")


def test_close_failure_is_nonfatal_and_does_not_leak_private_error(
    tmp_path, monkeypatch, capsys
):
    logger = configure_logging("hermes", path=tmp_path / "app.log")
    logger.info("before")
    descriptor = implementation._FILES["hermes"].descriptor
    real_close = os.close

    def failed_close(fd):
        real_close(fd)
        if fd == descriptor:
            raise OSError("private close error")

    with monkeypatch.context() as patch:
        patch.setattr(implementation.os, "close", failed_close)
        configure_logging("hermes", path=tmp_path / "new.log")
    logger.info("after")
    assert "private close error" not in capsys.readouterr().err
    assert (tmp_path / "new.log").read_text().endswith("after\n")


def test_stderr_text_stream_and_unavailable_stderr(tmp_path, monkeypatch):
    sink = io.StringIO()
    monkeypatch.setattr(sys, "stderr", sink)
    logger = get_logger("hermes")
    logger.info("text stream")
    assert sink.getvalue() == (tmp_path / "hermes.log").read_text()
    sink.close()
    logger.info("closed stderr")
    configure_logging("ares", path=tmp_path / "missing/app.log").error(
        "closed stderr diagnostic"
    )
    assert (tmp_path / "hermes.log").read_text().endswith("closed stderr\n")


def test_stderr_bytes_are_utf8_even_with_ascii_wrapper(tmp_path, monkeypatch):
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="ascii")
    monkeypatch.setattr(sys, "stderr", stream)
    get_logger("hermes").info("検証")
    assert buffer.getvalue() == (tmp_path / "hermes.log").read_bytes()


def test_path_change_during_dispatch_does_not_drop_record(tmp_path, capsys):
    entered, resume = Event(), Event()

    class PausingHandler(logging.Handler):
        def emit(self, record):
            entered.set()
            assert resume.wait(5), "reconfiguration did not release logging"

    logger = logging.getLogger("hermes")
    logger.addHandler(PausingHandler())
    old_path, new_path = tmp_path / "old.log", tmp_path / "new.log"
    configure_logging("hermes", path=old_path)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(logger.info, "in flight")
        try:
            assert entered.wait(5), "logging did not reach the handler"
            configure_logging("hermes", path=new_path)
        finally:
            resume.set()
        pending.result(timeout=5)
    assert new_path.read_text().endswith("in flight\n")
    assert not old_path.exists()
    assert capsys.readouterr().err.count("in flight") == 1


def test_formatting_does_not_invoke_application_record_factory(tmp_path):
    previous_factory = logging.getLogRecordFactory()
    calls = []

    def factory(*args, **kwargs):
        record = previous_factory(*args, **kwargs)
        calls.append(record.msg)
        return record

    logging.setLogRecordFactory(factory)
    try:
        get_logger("hermes").info("event %s", "one")
    finally:
        logging.setLogRecordFactory(previous_factory)
    assert calls == ["event %s"]
    assert (tmp_path / "hermes.log").read_text().endswith("event one\n")


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses DAC read permissions")
def test_directory_needs_search_and_write_permission_but_not_read(tmp_path, capsys):
    directory = tmp_path / "logs"
    directory.mkdir(mode=0o300)
    path = directory / "app.log"
    try:
        configure_logging("hermes", path=path).info("writable destination")
        assert path.read_text().endswith("writable destination\n")
        assert "file output unavailable" not in capsys.readouterr().err
    finally:
        directory.chmod(0o700)
