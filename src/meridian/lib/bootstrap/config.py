"""Read-only bootstrap config loading helpers."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.config.settings import MeridianConfig
from meridian.lib.config.settings import load_config as _load_config

if TYPE_CHECKING:
    from meridian.lib.ops.runtime import RuntimeAuthoritySnapshot


def load_config(
    project_root: Path,
    *,
    authority: RuntimeAuthoritySnapshot | None = None,
) -> MeridianConfig | None:
    """Load Meridian config for startup bootstrap without state mutation."""

    return _load_config(project_root, authority=authority)
