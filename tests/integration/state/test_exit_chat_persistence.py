"""Exit-chat reuse and creation share the same native persistence requirement."""

from pathlib import Path

import pytest

from meridian.lib.state import session_store


@pytest.mark.parametrize("target", ["entry", "other", "new"])
@pytest.mark.parametrize("persisted", [False, True])
def test_exit_chat_always_requires_native_proof(tmp_path: Path, target: str, persisted: bool):
    runtime = tmp_path / ".meridian"
    store = str(tmp_path / "native")
    chats: dict[str, str] = {}
    for native_id in ("entry", "other"):
        chats[native_id] = session_store.start_session(
            runtime, harness="claude", harness_session_id=native_id, model="",
            native_store=store,
        )
        session_store.stop_session(runtime, chats[native_id])
    checked: list[bool] = []

    def native_exists() -> bool:
        checked.append(True)
        return persisted

    result = session_store.get_or_create_exit_chat(
        runtime, chats["entry"], "claude", store, target, native_exists=native_exists,
    )
    assert checked == [True]
    if not persisted:
        assert result is None
        assert len(session_store.list_all_session_records(runtime)) == 2
    elif target in chats:
        assert result == chats[target]
    else:
        assert result is not None and result not in chats.values()
        record = session_store.get_session_record(runtime, result)
        assert record is not None and record.harness_session_id == target
        assert record.stopped_at is not None
