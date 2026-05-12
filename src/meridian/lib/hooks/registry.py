"""Hook registry with deterministic event ordering."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.hooks.config import HOOK_SOURCE_PRECEDENCE, HooksConfig, load_hooks_config
from meridian.lib.hooks.types import Hook, HookEventName

if TYPE_CHECKING:
    from meridian.lib.ops.runtime import RuntimeAuthoritySnapshot

_SOURCE_RANK = {source: rank for rank, source in enumerate(HOOK_SOURCE_PRECEDENCE)}


class HookRegistry:
    """Resolves hooks and serves event-scoped lookups."""

    def __init__(
        self,
        project_root: Path,
        *,
        user_config: Path | None = None,
        authority: RuntimeAuthoritySnapshot | None = None,
        hooks_config: HooksConfig | None = None,
    ) -> None:
        self._project_root = project_root.expanduser().resolve()
        self._config = hooks_config or load_hooks_config(
            self._project_root,
            user_config=user_config,
            authority=authority,
        )
        self._hooks = {hook.name: hook for hook in self._config.hooks}
        self._by_event: dict[HookEventName, list[Hook]] = {}
        self._build_event_index()

    def _build_event_index(self) -> None:
        grouped: dict[HookEventName, list[tuple[int, Hook]]] = {}
        for declaration_index, hook in enumerate(self._config.hooks):
            if not hook.enabled:
                continue
            grouped.setdefault(hook.event, []).append((declaration_index, hook))

        by_event: dict[HookEventName, list[Hook]] = {}
        for event, hooks in grouped.items():
            ordered = sorted(
                hooks,
                key=lambda item: (
                    _SOURCE_RANK.get(item[1].source, len(_SOURCE_RANK)),
                    -item[1].priority,
                    item[0],
                ),
            )
            by_event[event] = [hook for _, hook in ordered]
        self._by_event = by_event

    def get_hook(self, name: str) -> Hook | None:
        """Return one hook by name if registered."""

        return self._hooks.get(name)

    def get_all_hooks(self) -> list[Hook]:
        """Return all effective hooks after precedence/override resolution."""

        return list(self._config.hooks)

    def get_hooks_for_event(self, event: HookEventName) -> list[Hook]:
        """Return enabled hooks for one event in deterministic execution order."""

        return list(self._by_event.get(event, []))
