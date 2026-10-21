"""Refresh executed-model metadata through the canonical native reader."""

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.types import HarnessSessionId
from meridian.lib.harness.model_observation import NativeModelReadContext, read_last_executed_model
from meridian.lib.ops.runtime import async_from_sync, resolve_roots_for_read
from meridian.lib.state.event_store import utc_now_iso
from meridian.lib.state.history_index import HistoryIndex
from meridian.lib.state.session_store import SessionModelObservationEvent, record_model_observation


class SessionModelRefreshInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    chat_id: str
    project_root: str | None = None


class SessionModelRefreshOutput(BaseModel):
    chat_id: str
    model: str | None = None
    refreshed: bool = False


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
    token = read_last_executed_model(
        record.harness,
        key.session_id,
        context=NativeModelReadContext(
            native_store=record.native_store,
            project_root=record.control_root,
        ),
    )
    if token is not None:
        record_model_observation(
            roots.runtime_root,
            SessionModelObservationEvent(
                harness=record.harness,
                harness_session_id=HarnessSessionId(key.session_id),
                observed_model_token=token,
                recorded_at=utc_now_iso(),
            ),
        )
    model = token or index.last_executed_models({(record.harness, key.session_id)}).get(
        (record.harness, key.session_id),
        record.model,
    )
    return SessionModelRefreshOutput(
        chat_id=payload.chat_id, model=model, refreshed=token is not None
    )


session_model_refresh = async_from_sync(session_model_refresh_sync)
