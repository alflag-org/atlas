"""Filesystem locations and directory provisioning owned by Atlas."""

from atlas_core.paths import AtlasPaths, get_paths

__all__ = ["AtlasPaths", "ensure_dirs", "get_paths"]


def ensure_dirs(paths: AtlasPaths) -> None:
    """Create Atlas-owned writable directories."""
    for path in (
        paths.home,
        paths.etc,
        paths.var,
        paths.runtimes,
        paths.runtimes / "python",
        paths.runtimes / "perl",
        paths.environments,
        paths.environments / "python",
        paths.environments / "perl",
        paths.shims,
        paths.launchers,
        paths.logs,
        paths.runtime_state,
        paths.context_dir,
    ):
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ValueError(f"Atlas path must be a directory: {path}")
        path.mkdir(parents=True, exist_ok=True)
