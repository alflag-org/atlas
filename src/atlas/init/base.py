"""Interface shared by native init-system adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..catalog import ServiceRef


class InitAdapter(Protocol):
    """Operations Atlas requires from an init-system implementation."""

    def validate(self, service: ServiceRef) -> None:
        """Validate source artifacts without changing host state."""

    def diff(self, service: ServiceRef) -> str:
        """Return a unified diff against installed artifacts."""

    def install(self, service: ServiceRef) -> list[Path]:
        """Atomically install artifacts and reload the init system."""

    def remove(self, service: ServiceRef) -> list[Path]:
        """Remove installed artifacts and reload the init system."""

    def reload(self) -> None:
        """Reload native init-system definitions."""
