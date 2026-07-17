from pathlib import Path

from pydantic import BaseModel

from meridian.lib.state.event_store import append_event, read_events


class _Event(BaseModel):
    id: int
    kind: str


def test_append_event_repairs_truncated_trailing_line(tmp_path: Path) -> None:
    data_path = tmp_path / "events.jsonl"
    lock_path = tmp_path / "events.lock"
    data_path.write_bytes(b'{"id":1,"kind":"start"}\n{"id":2,"kind":"torn"')

    append_event(data_path, lock_path, _Event(id=3, kind="stop"))

    contents = data_path.read_bytes()
    assert contents == b'{"id":1,"kind":"start"}\n{"id":3,"kind":"stop"}\n'
    assert read_events(data_path, _Event.model_validate) == [
        _Event(id=1, kind="start"),
        _Event(id=3, kind="stop"),
    ]
    assert contents.endswith(b"\n")
