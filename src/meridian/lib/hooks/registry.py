from __future__ import annotations

from meridian.lib.hooks.config import HOOK_SOURCE_PRECEDENCE, HooksConfig
from meridian.lib.hooks.types import Hook, HookEventName

_SOURCE_RANK = {source: rank for rank, source in enumerate(HOOK_SOURCE_PRECEDENCE)}


class HookRegistry:
    """Serves event-scoped lookups over loaded hook rows."""

    def __init__(self, hooks_config: HooksConfig) -> None:
        self._config = hooks_config
        self._hooks = {(hook.name, hook.event): hook for hook in self._config.hooks}
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

    def get_hook(self, name: str, event: HookEventName) -> Hook | None:

        return self._hooks.get((name, event))

    def get_hooks_named(self, name: str) -> list[Hook]:

        return [hook for hook in self._config.hooks if hook.name == name]

    def get_all_hooks(self) -> list[Hook]:

        return list(self._config.hooks)

    def get_hooks_for_event(self, event: HookEventName) -> list[Hook]:

        return list(self._by_event.get(event, []))
