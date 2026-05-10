"""Shared session-detection helpers that stay harness-agnostic."""

from pathlib import Path
from typing import Any, cast

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.bundle import get_bundle_registry
from meridian.lib.harness.registry import HarnessRegistry, get_default_harness_registry


def infer_harness_from_untracked_session_ref(
    project_root: Path,
    session_ref: str,
    *,
    registry: HarnessRegistry | None = None,
) -> HarnessId | None:
    """Detect which harness owns *session_ref* by querying registered adapters."""

    normalized = session_ref.strip()
    if not normalized:
        return None

    seen_harnesses: set[HarnessId] = set()
    for harness_id, bundle in get_bundle_registry().items():
        seen_harnesses.add(harness_id)
        owns_untracked = cast(
            "Any",
            getattr(bundle.adapter, "owns_untracked_session", None),
        )
        if callable(owns_untracked) and owns_untracked(
            project_root=project_root,
            session_ref=normalized,
        ):
            return harness_id

    active_registry = registry if registry is not None else get_default_harness_registry()
    for harness_id in active_registry.ids():
        if harness_id in seen_harnesses:
            continue
        try:
            adapter = active_registry.get_subprocess_harness(harness_id)
        except TypeError:
            continue
        if adapter.owns_untracked_session(project_root=project_root, session_ref=normalized):
            return harness_id
    return None
