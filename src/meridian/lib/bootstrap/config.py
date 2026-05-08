"""Read-only bootstrap config loading helpers."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from meridian.lib.config.settings import MeridianConfig
from meridian.lib.config.settings import load_config as _load_config
from meridian.lib.state.paths import load_context_config

if TYPE_CHECKING:
    from meridian.lib.ops.runtime import RuntimeAuthoritySnapshot


def load_config(
    project_root: Path,
    *,
    authority: RuntimeAuthoritySnapshot | None = None,
) -> MeridianConfig | None:
    """Load Meridian config for startup bootstrap without state mutation."""

    return _load_config(project_root, authority=authority)


def load_context_snapshot(project_root: Path) -> dict[str, Any] | None:
    """Load merged context config as serializable snapshot data, if configured."""

    context_config = load_context_config(project_root)
    if context_config is None:
        return None
    return context_config.model_dump(mode="json")
