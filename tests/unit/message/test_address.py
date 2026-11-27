from pathlib import Path

import pytest

from meridian.lib.message.address import (
    AddressError,
    PersonAddress,
    SessionAddress,
    parse_address,
)
from meridian.lib.state.saved_machines import SavedMachine


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("p20417", SessionAddress("p20417")),
        ("c20417", SessionAddress("c20417")),
        ("@alice", PersonAddress("@alice")),
        ("@T123/1731600123.000200", PersonAddress("@T123/1731600123.000200")),
    ],
)
def test_address_that_names_a_receiver(raw: str, expected: SessionAddress | PersonAddress) -> None:
    assert parse_address(raw) == expected


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        # A selector means a session, so it must never reach the people program.
        ("@latest", "usage"),
        ("@last-failed", "usage"),
        ("@last-completed", "usage"),
        ("@", "usage"),
        ("@a b", "usage"),
        ("@a\x00b", "usage"),
        ("@" + "x" * 256, "usage"),
        ("inbox", "usage"),
        ("some-machine/inbox", "usage"),
        # Never guessed to be a person: a mistyped session ID must not end up in Slack.
        ("p12x", "usage"),
        ("native-session-uuid", "usage"),
        ("a/b/c", "usage"),
        ("some-machine/native-id", "usage"),
    ],
)
def test_address_that_is_refused_has_a_kind(raw: str, kind: str) -> None:
    with pytest.raises(AddressError) as refused:
        parse_address(raw)
    assert refused.value.kind == kind


def test_person_address_does_not_cross_machines() -> None:
    with pytest.raises(AddressError) as refused:
        parse_address("@alice", machine=SavedMachine("some-machine", "host", "0" * 12))
    assert refused.value.kind == "usage"


def test_other_machine_refuses_an_unknown_name_and_a_third_machine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from meridian.cli.machine_forward import other_machine
    from meridian.lib.machines.resolve import MachineError

    monkeypatch.setenv("MERIDIAN_HOME", str(tmp_path))
    with pytest.raises(MachineError) as unknown:
        other_machine(["some-machine/c204"], option_machine=None, directory_explicit=False)
    assert unknown.value.kind == "not_found"

    (tmp_path / "machines.json").write_text(
        '{"v":1,"machines":[{"name":"x","ssh":"host","id":"111111111111"}]}', encoding="utf-8"
    )
    monkeypatch.setenv("_MERIDIAN_MACHINE_CALL", '{"machine":"far","machine_id":"0123456789ab"}')
    with pytest.raises(MachineError) as third:
        other_machine(
            ["x/c1"],
            option_machine=SavedMachine("x", "host", "1" * 12),
            directory_explicit=False,
        )
    assert third.value.kind == "refused"


def test_machine_caller_drops_fields_with_the_wrong_shape() -> None:
    from meridian.lib.machines.caller import parse_machine_caller

    good = {"machine": "far", "machine_id": "0123456789ab", "spawn_id": "p12", "reply_id": "c12"}
    caller = parse_machine_caller(good)
    assert caller is not None and caller.reply_id == "c12"
    bad = {**good, "spawn_id": "p1; rm", "reply_id": "$(x)", "project_root": "rel"}
    shaped = parse_machine_caller(bad)
    assert shaped is not None
    assert (shaped.spawn_id, shaped.reply_id, shaped.project_root) == (None, None, None)
    assert parse_machine_caller({**good, "machine_id": "xyz"}) is None
