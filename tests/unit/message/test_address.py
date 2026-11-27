import pytest

from meridian.lib.message.address import (
    AddressError,
    PersonAddress,
    SessionAddress,
    parse_address,
)


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
        # A machine part is step 4's seam: every name is unknown until saved machines exist.
        ("some-machine/c204", "not_found"),
    ],
)
def test_address_that_is_refused_has_a_kind(raw: str, kind: str) -> None:
    with pytest.raises(AddressError) as refused:
        parse_address(raw)
    assert refused.value.kind == kind


def test_person_address_does_not_cross_machines() -> None:
    with pytest.raises(AddressError) as refused:
        parse_address("@alice", machine="some-machine")
    assert refused.value.kind == "usage"
