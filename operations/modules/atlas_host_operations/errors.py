"""User-facing failures for host lifecycle operations."""

from __future__ import annotations


class HostOperationError(Exception):
    """Base error that may be reported without a traceback."""


class InputError(HostOperationError):
    """A host specification or explicit source is invalid."""


class PlanError(HostOperationError):
    """A host operation plan is invalid or stale."""


class SafetyError(HostOperationError):
    """A requested mutation did not pass its safety policy."""


class AdapterError(HostOperationError):
    """A provider or configurator could not perform an operation."""


class UnknownProviderResult(AdapterError):
    """A provider mutation may have completed and requires reconciliation."""


class RegistryError(HostOperationError):
    """Global Registry rejected or could not complete a request."""


class RegistryConflictError(RegistryError):
    """A revision, lock, or fencing-token conflict occurred."""


class RegistryAuthenticationError(RegistryError):
    """Global Registry authentication or authorization failed."""


class RegistryUnavailableError(RegistryError):
    """Global Registry did not return a usable response."""
