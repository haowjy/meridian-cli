from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.catalog.mars_cli import MarsAgentNotFoundError
from meridian.lib.launch.request import SessionRequest
from meridian.lib.ops.mars import mars_resolve_agent
from meridian.lib.state import spawn_store
from meridian.lib.state.session_identity import chat_owner_spawn

if TYPE_CHECKING:
    from pydantic import JsonValue


def fork_usage_seed(runtime_root: Path, session: SessionRequest) -> dict[str, JsonValue] | None:
    # Claude and Codex fork usage includes the source session's cumulative usage.
    source_session_id = session.requested_harness_session_id
    if not session.continue_fork or not session.forked_from_chat_id or not source_session_id:
        return None
    owner_id = chat_owner_spawn(runtime_root, session.forked_from_chat_id)
    source = spawn_store.get_spawn(runtime_root, str(owner_id)) if owner_id is not None else None
    if source is None or source.harness_session_id != source_session_id:
        return None
    terminal_total = source.terminal.harness_cumulative if source.terminal is not None else None
    return terminal_total or source.prior_usage.harness_cumulative


def require_replayed_agent(
    *, project_root: Path, agent: str, source_ref: str, command: str
) -> None:
    """Refuse a fork whose session's agent profile is gone, naming the fork that works.

    ``--fork`` replays the session's agent; ``--fork-fresh`` with ``-a`` picks another.
    Raises ``ValueError`` when Mars cannot answer.
    """
    try:
        mars_resolve_agent(project_root, agent)
    except MarsAgentNotFoundError as exc:
        raise FileNotFoundError(
            f"{exc}\nSession '{source_ref}' ran as agent '{agent}', and --fork keeps the "
            f"session's agent. Fork it with another agent: {command} --fork-fresh "
            f"{source_ref} -a <agent>"
        ) from exc


__all__ = ["fork_usage_seed", "require_replayed_agent"]
