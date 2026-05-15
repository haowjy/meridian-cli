import pytest

from meridian.cli.argv_normalization import (
    FORK_INFERENCE_ERROR,
    SELF_FORK_REF_SENTINEL,
    normalize_optional_value_flags,
    resolve_fork_ref,
)


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--fork"], ["--fork", SELF_FORK_REF_SENTINEL]),
        (["--fork", "p123"], ["--fork", "p123"]),
        (["--fork", "--bg"], ["--fork", SELF_FORK_REF_SENTINEL, "--bg"]),
        (["--fork=p123"], ["--fork", "p123"]),
        (["--fork="], ["--fork", SELF_FORK_REF_SENTINEL]),
        (["--fork-fresh"], ["--fork-fresh", SELF_FORK_REF_SENTINEL]),
        (
            ["spawn", "--fork-fresh", "-a", "reviewer"],
            ["spawn", "--fork-fresh", SELF_FORK_REF_SENTINEL, "-a", "reviewer"],
        ),
        (
            ["spawn", "--fork", "--", "literal"],
            ["spawn", "--fork", SELF_FORK_REF_SENTINEL, "--", "literal"],
        ),
    ],
)
def test_normalize_optional_value_flags(argv: list[str], expected: list[str]) -> None:
    assert normalize_optional_value_flags(argv) == expected


def test_resolve_fork_ref_self_sentinel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p42")
    assert resolve_fork_ref(SELF_FORK_REF_SENTINEL) == "p42"


def test_resolve_fork_ref_self_sentinel_without_context_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MERIDIAN_SPAWN_ID", raising=False)
    with pytest.raises(ValueError, match=FORK_INFERENCE_ERROR):
        resolve_fork_ref(SELF_FORK_REF_SENTINEL)
