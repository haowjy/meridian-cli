"""Hook and plugin hook context JSON work-block behavior."""

from __future__ import annotations

import json
from uuid import UUID, uuid4

from meridian.lib.hooks.types import HookContext
from meridian.plugin_api.types import HookContext as PluginHookContext


def _event_id() -> UUID:
    return uuid4()


def test_hook_context_to_json_includes_dir_only_work_block() -> None:
    ctx = HookContext.from_roots(
        project_root="/proj",
        runtime_root="/runtime",
        event_name="spawn.finalized",
        event_id=_event_id(),
        timestamp="2026-01-01T00:00:00Z",
        work_dir="/runtime/spawns/p1/work",
    )
    payload = json.loads(ctx.to_json())

    assert payload["work"] == {"id": None, "dir": "/runtime/spawns/p1/work"}


def test_plugin_hook_context_to_json_includes_dir_only_work_block() -> None:
    ctx = PluginHookContext.from_roots(
        project_root="/proj",
        runtime_root="/runtime",
        event_name="spawn.finalized",
        event_id=_event_id(),
        timestamp="2026-01-01T00:00:00Z",
        work_dir="/runtime/spawns/p1/work",
    )
    payload = json.loads(ctx.to_json())

    assert payload["work"] == {"id": None, "dir": "/runtime/spawns/p1/work"}
