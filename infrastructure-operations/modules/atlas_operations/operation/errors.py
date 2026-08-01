"""User-facing errors for reviewed operation commands."""


class OperationError(Exception):
    """Base error for an operation command that should not emit a traceback."""


class InputError(OperationError):
    """An explicit input or provider definition is invalid."""


class PlanError(OperationError):
    """A plan or evidence artifact is invalid."""


class SafetyError(OperationError):
    """A reviewed operation did not pass a safety gate."""


class ProviderError(OperationError):
    """A provider adapter could not complete its requested action."""
