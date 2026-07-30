"""User-facing Atlas errors with stable process exit codes."""

from __future__ import annotations


class AtlasError(Exception):
    """Base class for expected operator-facing failures."""

    exit_code = 2


class LockUnavailableError(AtlasError):
    """Raised when a non-blocking job lock cannot be acquired."""

    exit_code = 75
