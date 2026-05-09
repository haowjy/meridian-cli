"""Platform process-scope containment layer.

Public API re-exports.  Platform-specific adapters live in:
- posix.py        POSIX process-group termination
- windows_job.py  Windows Job Object termination
- fallback.py     Cross-platform psutil tree termination (degraded fallback)
"""

from __future__ import annotations

from meridian.lib.platform.process_scope.base import (
    CleanupResult,
    ProcessScopeSnapshot,
    ScopedProcessHandle,
)
from meridian.lib.platform.process_scope.fallback import (
    terminate_tree,
    terminate_tree_sync,
)

__all__ = [
    "CleanupResult",
    "ProcessScopeSnapshot",
    "ScopedProcessHandle",
    "terminate_tree",
    "terminate_tree_sync",
]
