"""Meridian Plugin API.

Stable interface for hooks and plugins.

The public surface is intentionally narrow — hook types and state helpers only.
Utility functions (git helpers, config, file locking) live in submodules and
are not re-exported here.  Import them directly when needed::

    from meridian.plugin_api.git import resolve_clone_path
    from meridian.plugin_api.fs import file_lock
    from meridian.plugin_api.config import get_user_config
"""

from __future__ import annotations

from meridian.plugin_api.state import (
    get_project_home,
    get_user_home,
)
from meridian.plugin_api.types import (
    NOTIFICATION_EVENTS,
    FailurePolicy,
    Hook,
    HookContext,
    HookEventName,
    HookOutcome,
    HookResult,
    Notification,
    SpawnKind,
)

__version__ = "3.0.0"

__all__ = [
    "NOTIFICATION_EVENTS",
    "FailurePolicy",
    "Hook",
    "HookContext",
    "HookEventName",
    "HookOutcome",
    "HookResult",
    "Notification",
    "SpawnKind",
    "__version__",
    "get_project_home",
    "get_user_home",
]
