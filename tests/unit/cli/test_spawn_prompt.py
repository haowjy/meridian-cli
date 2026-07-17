"""Spawn prompt source resolution contracts."""

from __future__ import annotations

import io
import sys

import pytest

from meridian.cli.spawn import _resolve_spawn_prompt


class _UnreadableStdin:
    def isatty(self) -> bool:
        raise AssertionError("stdin must not be inspected without --prompt-file -")

    def read(self) -> str:
        raise AssertionError("stdin must not be read without --prompt-file -")


def test_explicit_prompt_file_stdin_reads_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("prompt from stdin"))

    assert (
        _resolve_spawn_prompt(None, "-", has_files=False, is_continue=False) == "prompt from stdin"
    )


@pytest.mark.parametrize(
    ("has_files", "is_continue"),
    [(True, False), (False, True)],
)
def test_reference_or_continue_without_prompt_does_not_touch_stdin(
    monkeypatch: pytest.MonkeyPatch,
    *,
    has_files: bool,
    is_continue: bool,
) -> None:
    monkeypatch.setattr(sys, "stdin", _UnreadableStdin())

    assert (
        _resolve_spawn_prompt(
            None,
            None,
            has_files=has_files,
            is_continue=is_continue,
        )
        == ""
    )


def test_missing_required_prompt_does_not_touch_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "stdin", _UnreadableStdin())

    with pytest.raises(
        ValueError,
        match=r"prompt required: pass -p/--prompt or --prompt-file",
    ):
        _resolve_spawn_prompt(None, None, has_files=False, is_continue=False)
