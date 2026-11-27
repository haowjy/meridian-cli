"""A message delivered to a session ends its open idle stretch, as a turn starting does."""

from __future__ import annotations

from meridian.lib.state.idle_store import IdleState, IdleStore


def end_stretch_on_delivery(harness: str, session: str) -> None:
    """Close the session's open idle stretch: the delivered message starts a turn, so the
    cache is warm again and the stretch's push, warn and keepwarm no longer apply.

    The next turn that ends opens a new stretch. ``push_owed_since_ms`` is left alone: the
    human was not the one who returned.
    """

    def close(current: IdleState | None) -> tuple[IdleState | None, None]:
        if current is None or not current.stretch_open:
            return current, None
        return current.model_copy(update={"stretch_open": False}), None

    IdleStore().mutate(harness, session, close)
