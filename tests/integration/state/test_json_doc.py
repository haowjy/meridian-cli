from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from meridian.lib.platform.locking import lock_file
from meridian.lib.state.json_doc import mutate_model, read_model, write_model


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

    repaired, _ = mutate_model(
        path,
        _Document,
        lambda current: (_Document(value=(current.value if current else 0) + 1), None),
        lock=lock_file(tmp_path / "sidecar.lock"),
    )

    assert repaired == _Document(value=1)
    assert read_model(path, _Document) == repaired
    assert path.stat().st_mode & 0o777 == 0o600


def test_json_document_mutations_serialize_on_the_supplied_lock(tmp_path: Path) -> None:
    path = tmp_path / "sidecar.json"
    lock_path = tmp_path / "sidecar.lock"

    def increment() -> None:
        for _ in range(25):
            mutate_model(
                path,
                _Document,
                lambda current: (
                    _Document(value=(current.value if current else 0) + 1),
                    None,
                ),
                lock=lock_file(lock_path),
            )

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(increment) for _ in range(4)]
        for future in futures:
            future.result()

    assert read_model(path, _Document) == _Document(value=100)
