"""Whether Mars installed native agent definitions for a harness in this project."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import cast

from meridian.lib.catalog.mars_roots import resolve_mars_roots
from meridian.lib.core.types import HarnessId

logger = logging.getLogger(__name__)

_LEDGER_VERSION = 1


def agent_copy_enabled(project_root: Path, harness_id: HarnessId) -> bool:
    """Return whether Mars installed at least one agent natively into ``harness_id``.

    Reads ``.mars/native-agents.json``, which Mars rewrites on every sync with the
    harnesses each agent was installed into. That reflects every config layer
    (``mars.toml``, ``mars.local.toml``, user ``config.toml``) without re-implementing
    their merge. The harness's own agent tool is then the project's delegation
    mechanism, so Meridian leaves it usable.

    A missing, unreadable, malformed, or unknown-version file means not enabled,
    matching how ``.mars/models-merged.json`` is read.
    """

    ledger_path = resolve_mars_roots(project_root).primary / ".mars" / "native-agents.json"
    if not ledger_path.is_file():
        return False
    try:
        raw = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.debug("Failed to read %s", ledger_path, exc_info=True)
        return False
    if not isinstance(raw, dict):
        return False
    ledger = cast("dict[str, object]", raw)
    agents = ledger.get("agents")
    if ledger.get("version") != _LEDGER_VERSION or not isinstance(agents, dict):
        return False
    return any(
        isinstance(harnesses, list) and harness_id.value in cast("list[object]", harnesses)
        for harnesses in cast("dict[str, object]", agents).values()
    )


__all__ = ["agent_copy_enabled"]
