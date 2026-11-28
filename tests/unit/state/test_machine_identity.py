"""The machine's identity file is written once, never regenerated."""

import json
from pathlib import Path

import pytest

from meridian.lib.state.machine_identity import (
    MachineIdentityError,
    default_machine_name,
    machine_file,
    read_or_create_machine_identity,
    rename_machine,
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


def test_newer_format_says_upgrade_and_never_remove() -> None:
    """Pins what 0.28.0's reader does with a file a later build wrote: after the release
    this reader can't change, and "remove it" would give the machine a new ID."""

    path = machine_file()
    path.parent.mkdir(parents=True)
    text = json.dumps({"v": 2, "id": "0123456789ab", "name": "box", "shape": "new"})
    path.write_text(text, encoding="utf-8")

    with pytest.raises(MachineIdentityError) as raised:
        read_or_create_machine_identity()
    message = str(raised.value)
    assert "newer Meridian" in message and "upgrade" in message
    assert "remov" not in message.lower() and "delet" not in message.lower()
    with pytest.raises(MachineIdentityError, match="newer Meridian"):
        rename_machine("other")
    assert path.read_text(encoding="utf-8") == text


def test_rename_keeps_keys_this_build_does_not_know() -> None:
    """Pins the other half: a later build may add a key to format 1, and a rename by 0.28.0
    after a rollback must write it back."""

    path = machine_file()
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"v": 1, "id": "0123456789ab", "name": "box", "was": [], "future": {"a": 1}}),
        encoding="utf-8",
    )

    renamed = rename_machine("other")

    assert (renamed.id, renamed.name, renamed.was) == ("0123456789ab", "other", ("box",))
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "v": 1,
        "id": "0123456789ab",
        "name": "other",
        "was": ["box"],
        "future": {"a": 1},
    }
    assert read_or_create_machine_identity() == renamed
