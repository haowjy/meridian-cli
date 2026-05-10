"""Passthrough registry."""

from __future__ import annotations

from meridian.lib.core.types import HarnessId

from .base import PassthroughError, TuiPassthrough
from .claude import ClaudePassthrough
from .codex import CodexPassthrough
from .opencode import OpenCodePassthrough


def get_passthrough(harness_id: HarnessId) -> TuiPassthrough:
    """Return passthrough builder for one harness."""

    registry: dict[HarnessId, type[TuiPassthrough]] = {
        HarnessId.CLAUDE: ClaudePassthrough,
        HarnessId.CODEX: CodexPassthrough,
        HarnessId.OPENCODE: OpenCodePassthrough,
    }
    factory = registry.get(harness_id)
    if factory is None:
        raise PassthroughError(
            f"Managed primary attach is not supported for {harness_id.value}"
        )
    return factory()


__all__ = ["get_passthrough"]
