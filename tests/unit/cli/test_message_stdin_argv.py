import pytest

from meridian.cli.argv_normalization import canonicalize_argv


@pytest.mark.parametrize(
    "argv",
    [
        ["message", "c1", "-"],
        ["message", "c1", "--message", "-"],
        ["message", "c1", "--message=-"],
        ["spawn", "message", "c1", "--message", "-"],
    ],
)
def test_every_spelling_of_stdin_text_reaches_the_parser_as_message_equals_dash(
    argv: list[str],
) -> None:
    assert "--message=-" in canonicalize_argv(argv)
    assert "-" not in canonicalize_argv(argv)
    assert "--message" not in canonicalize_argv(argv)
