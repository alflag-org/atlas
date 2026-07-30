"""Atlas domain errors with stable CLI exit semantics."""


class AtlasError(ValueError):
    """A user-facing Atlas failure with an explicit exit code."""

    exit_code = 1


class LockUnavailableError(AtlasError):
    """A non-blocking job lock could not be acquired."""

    exit_code = 75
