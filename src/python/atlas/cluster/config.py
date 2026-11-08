"""Strict host-owned cluster configuration; no automatic interface selection."""

from dataclasses import dataclass
from pathlib import Path

import yaml

from atlas_core._yaml import load_yaml_file
from atlas_core.cluster import ClusterError, PeerEndpoint

from .model import endpoint, text


@dataclass(frozen=True)
class Config:
    bind: PeerEndpoint
    advertise: PeerEndpoint
    credentials_file: Path
    seeds: tuple[PeerEndpoint, ...] = ()
    labels: dict[str, str] | None = None


def load_config(path: Path) -> Config:
    try:
        raw = load_yaml_file(path)
    except (yaml.YAMLError, ValueError, TypeError):
        raise ClusterError("invalid_configuration") from None
    if (
        not isinstance(raw, dict)
        or raw.keys()
        - {
            "bind",
            "advertise",
            "credentials_file",
            "seeds",
            "labels",
        }
        or not {"bind", "advertise", "credentials_file"} <= raw.keys()
    ):
        raise ClusterError("invalid_configuration")
    credentials = Path(text(raw["credentials_file"], 4096))
    seeds = raw.get("seeds", [])
    labels = raw.get("labels", {})
    if (
        not credentials.is_absolute()
        or ".." in credentials.parts
        or not isinstance(seeds, list)
        or len(seeds) > 32
        or not isinstance(labels, dict)
        or len(labels) > 16
    ):
        raise ClusterError("invalid_configuration")
    return Config(
        endpoint(raw["bind"], bind=True),
        endpoint(raw["advertise"]),
        credentials,
        tuple(endpoint(seed) for seed in seeds),
        {text(key, 64): text(value, 128, empty=True) for key, value in labels.items()},
    )
