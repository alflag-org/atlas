"""Native init-system adapters."""

from .systemd import SystemdAdapter

__all__ = ["SystemdAdapter"]
