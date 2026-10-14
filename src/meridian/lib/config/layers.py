"""Shared discovery and reading of layered Meridian config files."""

from __future__ import annotations

import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from meridian.lib.config.project_config_state import resolve_project_config_state
from meridian.lib.config.project_paths import (
    ProjectConfigPaths,
    resolve_project_config_paths,
)
from meridian.lib.config.project_root import resolve_user_config_path
from meridian.lib.utils.json_shapes import is_json_object

ConfigLayerName = Literal["user", "project", "local"]


class ConfigLayerAuthority(Protocol):
    """Project authority fields needed for config-layer discovery."""

    project_root: Path
    project_config_paths: ProjectConfigPaths


@dataclass(frozen=True)
class ConfigLayer:
    """One existing config file in ascending precedence order."""

    name: ConfigLayerName
    path: Path
    payload: dict[str, object]


def read_config_payload(path: Path) -> dict[str, object]:
    """Read one Meridian TOML layer as an object-shaped payload."""

    payload: object = tomllib.loads(path.read_text(encoding="utf-8"))
    if not is_json_object(payload):
        raise ValueError(f"Invalid TOML document in '{path}'.")
    return payload


def iter_config_layers(
    project_root: Path,
    *,
    authority: ConfigLayerAuthority | None = None,
    user_config: Path | None = None,
) -> Iterator[ConfigLayer]:
    """Yield user, project, and local config files from low to high precedence."""

    resolved_root = (
        authority.project_root.expanduser().resolve()
        if authority is not None
        else project_root.expanduser().resolve()
    )
    paths = (
        authority.project_config_paths
        if authority is not None
        else resolve_project_config_paths(resolved_root)
    )
    resolved_user = resolve_user_config_path(user_config)
    project_config = resolve_project_config_state(
        resolved_root,
        project_paths=paths,
    ).path

    candidates: tuple[tuple[ConfigLayerName, Path | None], ...] = (
        ("user", resolved_user),
        ("project", project_config),
        (
            "local",
            paths.meridian_local_toml if paths.meridian_local_toml.is_file() else None,
        ),
    )
    for name, path in candidates:
        if path is not None:
            yield ConfigLayer(name=name, path=path, payload=read_config_payload(path))
