import re

import pytest

from meridian.cli.argv_normalization import (
    FORK_IDENTITY_ERROR,
    FORK_INFERENCE_ERROR,
    FROM_INFERENCE_ERROR,
    SELF_FORK_REF_SENTINEL,
    ForkModeResolution,
    canonicalize_argv,
    normalize_optional_value_flags,
    resolve_fork_ref,
    resolve_optional_ref,
    validate_fork_mode,
)
from meridian.cli.bootstrap import iter_positional_tokens
from meridian.cli.startup.catalog import COMMAND_CATALOG
from meridian.cli.startup.classify import classify_invocation
from meridian.cli.startup.policy import StartupClass, StateRequirement


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--fork"], ["--fork", SELF_FORK_REF_SENTINEL]),
        (["--fork", "p123"], ["--fork", "p123"]),
        (["--fork", "--bg"], ["--fork", SELF_FORK_REF_SENTINEL, "--bg"]),
        (["--fork=p123"], ["--fork", "p123"]),
        (
            ["spawn", "--fork", "--", "literal"],
            ["spawn", "--fork", SELF_FORK_REF_SENTINEL, "--", "literal"],
        ),
    ],
)
def test_normalize_optional_value_flags(argv: list[str], expected: list[str]) -> None:
    assert normalize_optional_value_flags(argv) == expected


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--continue"], ["session", "browse"]),
        (["--continue="], ["session", "browse"]),
        (["--continue", "--plain"], ["session", "browse", "--plain"]),
        (["-C", "/tmp/project", "--continue"], ["session", "browse", "-C", "/tmp/project"]),
        (["--continue", "c9"], ["--continue", "c9"]),
        (["--", "--continue"], ["--", "--continue"]),
        (["spawn", "--", "--continue"], ["spawn", "--", "--continue"]),
        (["mars", "--continue"], ["mars", "--continue"]),
    ],
)
def test_canonicalize_argv_routes_bare_continue(argv: list[str], expected: list[str]) -> None:
    canonical = canonicalize_argv(argv)

    assert canonical == expected
    assert canonicalize_argv(canonical) == expected


def test_bare_continue_classifies_as_runtime_read() -> None:
    descriptor = classify_invocation(canonicalize_argv(["--continue"]), COMMAND_CATALOG)

    assert descriptor is not None
    assert descriptor.command_path == ("session", "browse")
    assert descriptor.startup_class is StartupClass.READ_RUNTIME
    assert descriptor.bootstrap_plan.state_requirement is StateRequirement.RUNTIME_READ


def test_positional_scanner_stops_at_passthrough_separator() -> None:
    assert list(iter_positional_tokens(["spawn", "--", "--format", "json"])) == [(0, "spawn")]
    assert list(iter_positional_tokens(["--format", "--", "spawn"])) == []


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


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            {"fork_from": "p1", "fork_fresh_from": "p2", "continue_from": None},
            "Cannot combine --fork with --fork-fresh.",
        ),
        (
            {"fork_from": "p1", "fork_fresh_from": None, "continue_from": "c1"},
            "Cannot combine --fork with --continue.",
        ),
        (
            {
                "fork_from": None,
                "fork_fresh_from": None,
                "continue_from": "p1",
                "context_from": ("c1",),
            },
            "Cannot combine --from with --continue.",
        ),
        (
            {
                "fork_from": "p1",
                "fork_fresh_from": None,
                "continue_from": None,
                "context_from": ("c1",),
            },
            "Cannot combine --fork with --from (MVP limitation).",
        ),
        (
            {
                "fork_from": "p1",
                "fork_fresh_from": None,
                "continue_from": None,
                "agent": "reviewer",
            },
            FORK_IDENTITY_ERROR,
        ),
    ],
)
def test_validate_fork_mode_rejects_conflicting_inputs(
    kwargs: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        validate_fork_mode(**kwargs)  # type: ignore[arg-type]


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
