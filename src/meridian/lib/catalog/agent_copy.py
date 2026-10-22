"""Whether Mars installs native agent definitions for a harness in this project."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import cast

from meridian.lib.catalog.mars_roots import resolve_mars_roots
from meridian.lib.core.types import HarnessId, normalize_mars_target_name


def agent_copy_enabled(project_root: Path, harness_id: HarnessId) -> bool:
    """Return whether effective Mars config copies native agents into ``harness_id``'s target.

    True when ``[settings.meridian.agent_copy] harnesses`` lists the harness and the harness
    is one of the Mars ``targets`` (or the ``managed_root``). The harness's own agent tool
    is then the project's delegation mechanism, so Meridian leaves it usable.
    """

    harness = harness_id.value
    mars_root = resolve_mars_roots(project_root).primary
    targets: tuple[str, ...] = ()
    agent_copy_harnesses: tuple[str, ...] = ()
    for config_name in ("mars.toml", "mars.local.toml"):
        config_path = mars_root / config_name
        if not config_path.is_file():
            continue
        with config_path.open("rb") as handle:
            payload = tomllib.load(handle)
        settings = cast("object", payload.get("settings"))
        if not isinstance(settings, dict):
            continue
        typed_settings = cast("dict[str, object]", settings)
        raw_targets = typed_settings.get("targets")
        if isinstance(raw_targets, list):
            targets = tuple(
                normalize_mars_target_name(str(target))
                for target in cast("list[object]", raw_targets)
            )
        raw_managed_root = typed_settings.get("managed_root")
        if not targets and isinstance(raw_managed_root, str):
            normalized_root = normalize_mars_target_name(raw_managed_root)
            if normalized_root:
                targets = (normalized_root,)
        meridian_table = typed_settings.get("meridian")
        if not isinstance(meridian_table, dict):
            continue
        agent_copy = cast("dict[str, object]", meridian_table).get("agent_copy")
        if not isinstance(agent_copy, dict) or "harnesses" not in agent_copy:
            continue
        harnesses = cast("dict[str, object]", agent_copy).get("harnesses")
        if isinstance(harnesses, list):
            agent_copy_harnesses = tuple(
                str(name).strip().lower() for name in cast("list[object]", harnesses)
            )
    return harness in agent_copy_harnesses and harness in targets


__all__ = ["agent_copy_enabled"]
