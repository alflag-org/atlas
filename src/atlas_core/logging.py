"""Shared application diagnostics, separate from Atlas completed-run records."""

from __future__ import annotations

import errno
import logging
import os
import re
import stat
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from copy import copy
from datetime import UTC, datetime
from pathlib import Path

from .context import get_context

__all__ = ["configure_logging", "get_logger"]

_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}
_DEFAULT_DIRECTORY = Path("/var/log/atlas")
_CONFIGURATION_LOCK = threading.RLock()
_FILES: dict[str, _FileHandler] = {}
_FAILURE_LOCK = threading.Lock()
_FAILED_PATHS: set[Path] = set()


def _warn(message: str) -> None:
    # Never recurse through logging, or fail the application if stderr is unavailable.
    try:
        sys.stderr.write(f"atlas logging: {message}\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


def _namespace(name: str) -> str:
    if not isinstance(name, str):
        raise TypeError("logger name must be a string")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*", name):
        raise ValueError("invalid logger name")
    namespace = name.split(".", 1)[0]
    if namespace == "root":
        raise ValueError("root logger is not an application namespace")
    return namespace


def _level(value: str | int) -> int:
    if isinstance(value, str):
        value = _LEVELS.get(value.upper(), -1)
    if type(value) is not int or value not in _LEVELS.values():
        raise ValueError("level must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
    return value


def _initial_level() -> int:
    value = os.environ.get("ATLAS_LOG_LEVEL")
    if value is None:
        return logging.INFO
    try:
        return _level(value)
    except ValueError:
        _warn("invalid ATLAS_LOG_LEVEL; using INFO")
        return logging.INFO


def _explicit_path(value: str | os.PathLike[str]) -> Path:
    path = Path(value)
    has_control_character = any(
        ord(char) < 32 or ord(char) == 127 for char in str(path)
    )
    if not path.is_absolute() or ".." in path.parts or has_control_character:
        raise ValueError(
            "log path must be absolute without traversal or control characters"
        )
    return path


def _context() -> tuple[str | None, dict[str, str]]:
    program = os.environ.get("ATLAS_PROGRAM_NAME")
    fields = {}
    for key, variable in (
        ("host_id", "ATLAS_HOST_ID"),
        ("run_id", "ATLAS_RUN_ID"),
        ("operation_id", "ATLAS_OPERATION_ID"),
    ):
        value = os.environ.get(variable)
        if value:
            fields[key] = value

    # Reading an existing execution context must never require standalone host.yml.
    if not os.environ.get("ATLAS_CONTEXT_FILE"):
        return program, fields
    try:
        context = get_context()
    except (OSError, ValueError, RuntimeError, TypeError, KeyError, AttributeError):
        return program, fields
    program = program or context.program.name
    fields = {
        "host_id": context.host.id,
        "run_id": context.execution.run_id,
        "operation_id": context.execution.operation_id,
    }
    return program, fields


def _default_path(program: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", program) or ".." in program:
        raise ValueError("invalid Atlas program name for log path")
    return _DEFAULT_DIRECTORY / f"{program}.log"


class _Formatter(logging.Formatter):
    def __init__(self, fields: dict[str, str]) -> None:
        # Escape context whitespace so identifiers cannot inject extra log lines.
        context = " ".join(f"{key}={value!s}" for key, value in fields.items())
        self.context = context.translate(
            str.maketrans({"\n": r"\n", "\r": r"\r", "\t": r"\t"})
        )
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return (
            datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )

    def format(self, record: logging.LogRecord) -> str:
        # Format a copy: user handlers and other namespaces retain the original record.
        copied = copy(record)
        if self.context:
            copied.msg = f"{self.context} {record.getMessage()}"
            copied.args = ()
        return super().format(copied)


class _StderrHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        text = self.format(record) + "\n"
        try:
            buffer = getattr(sys.stderr, "buffer", None)
            if buffer is None:
                sys.stderr.write(text)
            else:
                buffer.write(text.encode("utf-8", errors="backslashreplace"))
            sys.stderr.flush()
        except (OSError, ValueError):
            pass


@contextmanager
def _parent_directory(path: Path) -> Iterator[int]:
    # Walk using directory descriptors, rejecting symlinks at every component.
    # Traversal needs search permission, not permission to list directory entries.
    flags = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(path.anchor, flags)
    try:
        for component in path.parts[1:-1]:
            child = os.open(component, flags, dir_fd=descriptor)
            previous, descriptor = descriptor, child
            os.close(previous)
        yield descriptor
    finally:
        os.close(descriptor)


class _FileHandler(logging.Handler):
    """Unbuffered append with safe reopen, outage suppression, and no replay queue."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        self.descriptor: int | None = None
        self.identity: tuple[int, int] | None = None

    def _close_file(self) -> None:
        if self.descriptor is not None:
            descriptor, self.descriptor = self.descriptor, None
            try:
                os.close(descriptor)
            except OSError as error:
                self._failure(error)

    def _failure(self, error: OSError) -> None:
        with _FAILURE_LOCK:
            if self.path not in _FAILED_PATHS:
                # strerror contains the OS category, never an arbitrary exception body.
                reason = os.strerror(error.errno or errno.EIO)
                _warn(
                    f"file output unavailable: {self.path} ({reason}); continuing on stderr"
                )
                _FAILED_PATHS.add(self.path)

    def _open(self, parent: int) -> None:
        flags = (
            os.O_WRONLY
            | os.O_APPEND
            | os.O_CREAT
            | os.O_NOFOLLOW
            | os.O_NONBLOCK
            | os.O_CLOEXEC
        )
        descriptor = os.open(self.path.name, flags, 0o640, dir_fd=parent)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise OSError(errno.EINVAL, "log destination must be a regular file")
        except BaseException:
            os.close(descriptor)
            raise
        self.descriptor = descriptor
        self.identity = info.st_dev, info.st_ino

    def set_path(self, path: Path) -> None:
        # Keep the handler attached: a dispatch already in progress must not lose its record.
        with self.lock:
            if path == self.path:
                return
            self._close_file()
            self.path = path
            self.identity = None

    def _refresh_file(self, parent: int) -> None:
        try:
            info = os.stat(self.path.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            identity = None
        else:
            if not stat.S_ISREG(info.st_mode):
                raise OSError(errno.EINVAL, "log destination must be a regular file")
            identity = info.st_dev, info.st_ino
        if identity != self.identity:
            self._close_file()
        if self.descriptor is None:
            self._open(parent)

    def emit(self, record: logging.LogRecord) -> None:
        data = (self.format(record) + "\n").encode("utf-8", errors="backslashreplace")
        try:
            with _parent_directory(self.path) as parent:
                self._refresh_file(parent)
            # os.write may be short even for a regular file; no bytes survive this emit.
            remaining = memoryview(data)
            while remaining:
                written = os.write(self.descriptor, remaining)
                if written == 0:
                    raise OSError(errno.EIO, "log write made no progress")
                remaining = remaining[written:]
        except OSError as error:
            self._failure(error)
            self._close_file()
        else:
            with _FAILURE_LOCK:
                _FAILED_PATHS.discard(self.path)

    def close(self) -> None:
        with self.lock:
            try:
                self._close_file()
            finally:
                super().close()


def get_logger(name: str) -> logging.Logger:
    """Return a shared stdlib Logger, initializing its namespace once.

    The first enabled record opens the default file. Repeated retrieval preserves
    explicit levels and paths, including levels set directly on child loggers.
    """
    namespace = _namespace(name)
    _configure(namespace, level=None, path=None)
    return logging.getLogger(name)


def configure_logging(
    name: str,
    *,
    level: str | int | None = None,
    path: str | os.PathLike[str] | None = None,
) -> logging.Logger:
    """Configure a top-level namespace; omitted values retain existing settings.

    Initially use ATLAS_LOG_LEVEL or INFO, and /var/log/atlas/<program>.log.
    Explicit paths must be absolute. Invalid arguments raise TypeError or
    ValueError; file I/O failures warn on stderr and retry on later records.
    """
    namespace = _namespace(name)
    if namespace != name:
        raise ValueError("configure_logging requires a top-level namespace")
    selected_level = None if level is None else _level(level)
    selected_path = None if path is None else _explicit_path(path)
    return _configure(namespace, level=selected_level, path=selected_path)


def _configure(name: str, *, level: int | None, path: Path | None) -> logging.Logger:
    with _CONFIGURATION_LOCK:
        logger = logging.getLogger(name)
        handler = _FILES.get(name)
        if handler is not None:
            if path is not None:
                handler.set_path(path)
            if level is not None:
                logger.setLevel(level)
            return logger

        program, fields = _context()
        selected_path = path if path is not None else _default_path(program or name)
        selected_level = _initial_level() if level is None else level
        formatter = _Formatter(fields)
        handler = _FileHandler(selected_path)
        handler.setFormatter(formatter)
        stderr = _StderrHandler()
        stderr.setFormatter(formatter)
        logger.addHandler(stderr)
        logger.addHandler(handler)
        logger.setLevel(selected_level)
        logger.propagate = False
        _FILES[name] = handler
        return logger
