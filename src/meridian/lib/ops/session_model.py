"""Refresh executed-model metadata through the canonical native reader."""

from pydantic import BaseModel, ConfigDict

from meridian.lib.harness.model_observation import observe_session_model
from meridian.lib.ops.runtime import resolve_roots_for_read
from meridian.lib.state.history_index import HistoryIndex


class SessionModelRefreshInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    chat_id: str
    project_root: str | None = None


class SessionModelRefreshOutput(BaseModel):
    chat_id: str
    model: str | None = None


def session_model_refresh_sync(payload: SessionModelRefreshInput) -> SessionModelRefreshOutput:
    roots = resolve_roots_for_read(payload.project_root)
    if roots is None:
        return SessionModelRefreshOutput(chat_id=payload.chat_id)
    index = HistoryIndex(roots.runtime_root)
    records = index.sessions(chat_ids={payload.chat_id})
    if not records:
        return SessionModelRefreshOutput(chat_id=payload.chat_id)
    record = records[0]
    key = record.native_key()
    if key is None or record.record_mode == "historical":
        return SessionModelRefreshOutput(chat_id=payload.chat_id, model=record.model)
    token = observe_session_model(roots.runtime_root, key, project_root=record.control_root)
    model = token or index.last_executed_models({(record.harness, key.session_id)}).get(
        (record.harness, key.session_id),
        record.model,
    )
    return SessionModelRefreshOutput(chat_id=payload.chat_id, model=model)
