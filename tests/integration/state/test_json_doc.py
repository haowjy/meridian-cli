from pathlib import Path

from pydantic import BaseModel, ConfigDict

from meridian.lib.state.json_doc import read_model, write_model


class _Document(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    value: int


def test_json_document_round_trip_is_private_and_tolerant(tmp_path: Path) -> None:
    path = tmp_path / "sidecar.json"
    assert read_model(path, _Document) is None

    write_model(path, _Document(value=3))

    assert read_model(path, _Document) == _Document(value=3)
    assert path.stat().st_mode & 0o777 == 0o600

    path.write_text('{"value":', encoding="utf-8")
    assert read_model(path, _Document) is None
