"""Backward-compat shim — delegates to process_scope.fallback.

Existing callers (runner_helpers.py, etc.) continue to work unchanged.
The canonical implementation lives in platform/process_scope/fallback.py.
"""

from __future__ import annotations

import asyncio

from meridian.lib.platform.process_scope.fallback import terminate_tree as _terminate_tree


async def terminate_tree(
    process: asyncio.subprocess.Process,
    *,
    grace_secs: float = 5.0,
) -> None:
    """Terminate a subprocess tree, escalating to kill after a grace period."""
    await _terminate_tree(process, grace_secs=grace_secs)


__all__ = ["terminate_tree"]
