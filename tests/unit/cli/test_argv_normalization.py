import re

import pytest

from meridian.cli.argv_normalization import (
    FORK_IDENTITY_ERROR,
    FORK_INFERENCE_ERROR,
    FROM_INFERENCE_ERROR,
    SELF_FORK_REF_SENTINEL,
    SYNTHETIC_VALUE_TOKENS,
    ForkModeResolution,
    normalize_optional_value_flags,
    resolve_fork_ref,
    resolve_optional_ref,
    validate_fork_mode,
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
        (["--from"], ["--from", SELF_FORK_REF_SENTINEL]),
        (["--from", "p123"], ["--from", "p123"]),
        (["--from=p123"], ["--from", "p123"]),
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


def test_resolve_optional_ref_self_sentinel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p42")
    assert resolve_optional_ref(SELF_FORK_REF_SENTINEL, flag_name="--from") == "p42"


def test_resolve_fork_ref_self_sentinel_without_context_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MERIDIAN_SPAWN_ID", raising=False)
    with pytest.raises(ValueError, match=FORK_INFERENCE_ERROR):
        resolve_fork_ref(SELF_FORK_REF_SENTINEL)


def test_resolve_optional_ref_self_sentinel_without_context_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MERIDIAN_SPAWN_ID", raising=False)
    with pytest.raises(ValueError, match=FROM_INFERENCE_ERROR):
        resolve_optional_ref(SELF_FORK_REF_SENTINEL, flag_name="--from")


def test_synthetic_value_tokens_contains_sentinel() -> None:
    assert SELF_FORK_REF_SENTINEL in SYNTHETIC_VALUE_TOKENS


def test_validate_fork_mode_conflicts_fork_and_fork_fresh() -> None:
    with pytest.raises(ValueError, match=re.escape("Cannot combine --fork with --fork-fresh.")):
        validate_fork_mode(fork_from="p1", fork_fresh_from="p2", continue_from=None)


def test_validate_fork_mode_conflicts_fork_and_continue() -> None:
    with pytest.raises(ValueError, match=re.escape("Cannot combine --fork with --continue.")):
        validate_fork_mode(fork_from="p1", fork_fresh_from=None, continue_from="c1")


def test_validate_fork_mode_conflicts_fork_fresh_and_continue() -> None:
    with pytest.raises(ValueError, match=re.escape("Cannot combine --fork-fresh with --continue.")):
        validate_fork_mode(fork_from=None, fork_fresh_from="p1", continue_from="c1")


def test_validate_fork_mode_conflicts_from_and_continue() -> None:
    with pytest.raises(ValueError, match=re.escape("Cannot combine --from with --continue.")):
        validate_fork_mode(
            fork_from=None,
            fork_fresh_from=None,
            continue_from="p1",
            context_from=("c1",),
        )


def test_validate_fork_mode_conflicts_fork_and_from() -> None:
    with pytest.raises(
        ValueError, match=re.escape("Cannot combine --fork with --from (MVP limitation).")
    ):
        validate_fork_mode(
            fork_from="p1",
            fork_fresh_from=None,
            continue_from=None,
            context_from=("c1",),
        )


def test_validate_fork_mode_conflicts_fork_fresh_and_from() -> None:
    with pytest.raises(
        ValueError, match=re.escape("Cannot combine --fork-fresh with --from (MVP limitation).")
    ):
        validate_fork_mode(
            fork_from=None,
            fork_fresh_from="p1",
            continue_from=None,
            context_from=("c1",),
        )


def test_validate_fork_mode_rejects_identity_override_agent() -> None:
    with pytest.raises(ValueError, match=re.escape(FORK_IDENTITY_ERROR)):
        validate_fork_mode(
            fork_from="p1",
            fork_fresh_from=None,
            continue_from=None,
            agent="reviewer",
        )


def test_validate_fork_mode_rejects_identity_override_model() -> None:
    with pytest.raises(ValueError, match=re.escape(FORK_IDENTITY_ERROR)):
        validate_fork_mode(
            fork_from="p1",
            fork_fresh_from=None,
            continue_from=None,
            model="gpt-5.4-mini",
        )


def test_validate_fork_mode_rejects_identity_override_skills() -> None:
    with pytest.raises(ValueError, match=re.escape(FORK_IDENTITY_ERROR)):
        validate_fork_mode(
            fork_from="p1",
            fork_fresh_from=None,
            continue_from=None,
            skills="foo,bar",
        )


def test_validate_fork_mode_allows_fork_fresh_with_agent_model_and_skills() -> None:
    assert validate_fork_mode(
        fork_from=None,
        fork_fresh_from="p1",
        continue_from=None,
        agent="reviewer",
        model="gpt-5.4-mini",
        skills="foo,bar",
    ) == ForkModeResolution(
        fork_ref=None, fork_fresh_ref="p1", is_fork=True, is_fresh=True, resolved_context_from=()
    )


def test_validate_fork_mode_no_fork_no_continue_returns_empty_resolution() -> None:
    assert validate_fork_mode(
        fork_from=None,
        fork_fresh_from=None,
        continue_from=None,
    ) == ForkModeResolution(
        fork_ref=None, fork_fresh_ref=None, is_fork=False, is_fresh=False, resolved_context_from=()
    )


def test_validate_fork_mode_resolves_fork_ref() -> None:
    assert validate_fork_mode(
        fork_from="p123",
        fork_fresh_from=None,
        continue_from=None,
    ) == ForkModeResolution(
        fork_ref="p123", fork_fresh_ref=None, is_fork=True, is_fresh=False, resolved_context_from=()
    )


def test_validate_fork_mode_resolves_fork_fresh_ref() -> None:
    assert validate_fork_mode(
        fork_from=None,
        fork_fresh_from="p456",
        continue_from=None,
    ) == ForkModeResolution(
        fork_ref=None, fork_fresh_ref="p456", is_fork=True, is_fresh=True, resolved_context_from=()
    )


def test_validate_fork_mode_resolves_sentinel_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p42")
    assert validate_fork_mode(
        fork_from=SELF_FORK_REF_SENTINEL,
        fork_fresh_from=None,
        continue_from=None,
    ) == ForkModeResolution(
        fork_ref="p42", fork_fresh_ref=None, is_fork=True, is_fresh=False, resolved_context_from=()
    )


def test_validate_fork_mode_resolves_context_from_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p42")
    assert validate_fork_mode(
        fork_from=None,
        fork_fresh_from=None,
        continue_from=None,
        context_from=(SELF_FORK_REF_SENTINEL, "c1"),
    ) == ForkModeResolution(
        fork_ref=None,
        fork_fresh_ref=None,
        is_fork=False,
        is_fresh=False,
        resolved_context_from=("p42", "c1"),
    )
