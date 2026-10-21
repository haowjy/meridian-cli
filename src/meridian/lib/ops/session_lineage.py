"""Indexed top-level conversation relationships, excluding subagent ownership."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from meridian.lib.ops.runtime import async_from_sync, resolve_roots_for_read
from meridian.lib.state.history_index import HistoryIndex


class SessionLineageInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    chat_id: str
    project_root: str | None = None


class SessionLineageEdge(BaseModel):
    child_chat_id: str
    parent_chat_id: str
    kind: Literal["fork", "from"]
    parent_history_id: str | None = None


class SessionLineageOutput(BaseModel):
    chat_id: str
    edges: tuple[SessionLineageEdge, ...] = ()


def session_lineage_sync(payload: SessionLineageInput) -> SessionLineageOutput:
    roots = resolve_roots_for_read(payload.project_root)
    edges = HistoryIndex(roots.runtime_root).lineage_edges(payload.chat_id) if roots else []
    return SessionLineageOutput(
        chat_id=payload.chat_id,
        edges=tuple(
            SessionLineageEdge(
                child_chat_id=child,
                parent_chat_id=parent,
                kind="fork" if kind == "forked" else "from",
                parent_history_id=history,
            )
            for child, kind, parent, history in edges
        ),
    )


session_lineage = async_from_sync(session_lineage_sync)
