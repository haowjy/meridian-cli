"""The machine's identity file is written once, never regenerated."""

import json
from pathlib import Path

import pytest

from meridian.lib.state.machine_identity import (
    MachineIdentityError,
    default_machine_name,
    machine_file,
    read_or_create_machine_identity,
)


@pytest.fixture(autouse=True)
def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MERIDIAN_HOME", (tmp_path / "home").as_posix())


@pytest.mark.parametrize(
    ("login", "host", "expected"),
    [
        ("jimyao", "pop-os", "jimyao-pop-os"),
        ("Jim.Yao", "Mini_Box", "jim-yao-mini-box"),
        ("1st", "box", "m1st-box"),
        ("a" * 80, "box", "a" * 63),
    ],
)
def test_default_name_fits_the_name_rule(login: str, host: str, expected: str) -> None:
    assert default_machine_name(login, host) == expected


def test_identity_is_created_once_and_kept() -> None:
    first = read_or_create_machine_identity()
    assert read_or_create_machine_identity() == first
    assert len(first.id) == 12
    assert json.loads(machine_file().read_text())["v"] == 1


def test_unreadable_file_is_named_not_regenerated() -> None:
    path = machine_file()
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(MachineIdentityError, match=r"machine\.json"):
        read_or_create_machine_identity()
    assert path.read_text(encoding="utf-8") == "{not json"
