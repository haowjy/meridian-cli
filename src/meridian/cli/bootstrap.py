"""Bootstrap helpers for meridian CLI startup behavior."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from meridian.cli.startup.policy import StateRequirement

# Keep these startup parse tables in sync with `@app.default root(...)` in
# `main.py` plus startup-only flags parsed before cyclopts (`--verbose` / `-v`).
_TOP_LEVEL_VALUE_FLAGS = frozenset(
    {
        "--format",
        "--config",
        "--continue",
        "--fork",
        "--model",
        "-m",
        "--harness",
        "-a",
        "--work",
        "--autocompact",
        "--effort",
        "--sandbox",
        "--approval",
        "--timeout",
    }
)
_TOP_LEVEL_BOOL_FLAGS = frozenset(
    {
        "--help",
        "-h",
        "--version",
        "--verbose",
        "-v",
        "--json",
        "--no-json",
        "--yes",
        "--no-yes",
        "--no-input",
        "--no-no-input",
        "--agent",
        "--human",
        "--yolo",
        "--no-yolo",
        "--dry-run",
        "--no-dry-run",
    }
)
HARNESS_SHORTCUT_NAMES = frozenset({"claude", "codex", "opencode"})
_CHAT_MANAGEMENT_SUBCOMMANDS = frozenset({"ls", "show", "log", "close"})


@dataclass(frozen=True)
class ParsedGlobalOptions:
    output_format: str
    config_file: str | None
    harness: str | None
    yes: bool
    no_input: bool
    output_explicit: bool
    force_agent: bool
    force_human: bool


def _first_positional_token_with_index(argv: Sequence[str]) -> tuple[int, str] | None:
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return None
        if not token.startswith("-"):
            return index, token
        if "=" in token:
            index += 1
            continue
        if token in _TOP_LEVEL_BOOL_FLAGS:
            index += 1
            continue
        if token in _TOP_LEVEL_VALUE_FLAGS:
            index += 2
            continue
        index += 1
    return None


def _first_positional_token(argv: Sequence[str]) -> str | None:
    resolved = _first_positional_token_with_index(argv)
    if resolved is None:
        return None
    _, token = resolved
    return token


def first_positional_token_with_index(argv: Sequence[str]) -> tuple[int, str] | None:
    return _first_positional_token_with_index(argv)


def first_positional_token(argv: Sequence[str]) -> str | None:
    return _first_positional_token(argv)


def split_passthrough_args(argv: Sequence[str]) -> tuple[list[str], tuple[str, ...]]:
    """Split args at ``--`` so cyclopts never sees passthrough tokens."""

    if "--" not in argv:
        return list(argv), ()
    sep_idx = list(argv).index("--")
    return list(argv[:sep_idx]), tuple(argv[sep_idx + 1 :])


def _is_chat_management_invocation(argv: Sequence[str]) -> bool:
    if len(argv) < 2:
        return False
    return argv[0] == "chat" and argv[1] in _CHAT_MANAGEMENT_SUBCOMMANDS


def extract_global_options(
    argv: Sequence[str],
    *,
    normalize_output_format: Callable[[str | None, bool], str],
) -> tuple[list[str], ParsedGlobalOptions]:
    json_mode = False
    output_format: str | None = None
    config_file: str | None = None
    harness: str | None = None
    yes = False
    no_input = False
    output_explicit = False
    force_agent = False
    force_human = False
    harness_source: str | None = None
    cleaned: list[str] = []

    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            cleaned.extend(argv[index:])
            break
        if arg == "mars":
            cleaned.extend(argv[index:])
            break
        if arg == "--json":
            json_mode = True
            output_explicit = True
            index += 1
            continue
        if arg == "--no-json":
            output_explicit = True
            index += 1
            continue
        if arg == "--format":
            if index + 1 >= len(argv):
                raise SystemExit("--format requires a value")
            output_format = argv[index + 1]
            output_explicit = True
            index += 2
            continue
        if arg.startswith("--format="):
            output_format = arg.partition("=")[2]
            output_explicit = True
            index += 1
            continue
        if arg == "--config":
            if index + 1 >= len(argv):
                raise SystemExit("--config requires a value")
            config_file = argv[index + 1].strip()
            if not config_file:
                raise SystemExit("--config requires a non-empty value")
            index += 2
            continue
        if arg.startswith("--config="):
            config_file = arg.partition("=")[2].strip()
            if not config_file:
                raise SystemExit("--config requires a non-empty value")
            index += 1
            continue
        if arg == "--harness":
            if index + 1 >= len(argv):
                raise SystemExit("--harness requires a value")
            requested_harness = argv[index + 1].strip()
            if not requested_harness:
                raise SystemExit("--harness requires a non-empty value")
            if harness is not None and harness != requested_harness:
                raise SystemExit(
                    f"Conflicting harness selections: '{harness}' and '{requested_harness}'."
                )
            harness = requested_harness
            harness_source = "--harness"
            index += 2
            continue
        if arg.startswith("--harness="):
            requested_harness = arg.partition("=")[2].strip()
            if not requested_harness:
                raise SystemExit("--harness requires a non-empty value")
            if harness is not None and harness != requested_harness:
                raise SystemExit(
                    f"Conflicting harness selections: '{harness}' and '{requested_harness}'."
                )
            harness = requested_harness
            harness_source = "--harness"
            index += 1
            continue
        if arg == "--yes":
            yes = True
            index += 1
            continue
        if arg == "--no-yes":
            index += 1
            continue
        if arg == "--no-input":
            no_input = True
            index += 1
            continue
        if arg == "--no-no-input":
            index += 1
            continue
        if arg == "--agent":
            if _first_positional_token(cleaned) is not None:
                if index + 1 < len(argv) and not argv[index + 1].startswith("-"):
                    cleaned.extend(("-a", argv[index + 1]))
                    index += 2
                else:
                    cleaned.append("-a")
                    index += 1
                continue
            force_agent = True
            index += 1
            continue
        if arg == "--human":
            force_human = True
            index += 1
            continue
        if arg in {"--verbose", "-v"} and _first_positional_token(cleaned) is None:
            index += 1
            continue

        cleaned.append(arg)
        index += 1

    shortcut = _first_positional_token_with_index(cleaned)
    if shortcut is not None:
        shortcut_index, shortcut_value = shortcut
        if shortcut_value in HARNESS_SHORTCUT_NAMES:
            if harness is not None and harness != shortcut_value:
                raise SystemExit(
                    "Conflicting harness selections: "
                    f"'{harness}' and '{shortcut_value}'."
                )
            harness = shortcut_value
            harness_source = shortcut_value
            del cleaned[shortcut_index]

    if harness is not None and _is_chat_management_invocation(cleaned):
        if harness_source == "--harness":
            raise SystemExit('Unknown option: "--harness"')
        if harness_source in HARNESS_SHORTCUT_NAMES:
            raise SystemExit(f'Unknown option: "{harness_source}"')

    if force_agent and force_human:
        raise SystemExit("Cannot combine --agent with --human.")

    return cleaned, ParsedGlobalOptions(
        output_format=normalize_output_format(output_format, json_mode),
        config_file=config_file,
        harness=harness,
        yes=yes,
        no_input=no_input,
        output_explicit=output_explicit,
        force_agent=force_agent,
        force_human=force_human,
    )


def validate_top_level_command(
    argv: Sequence[str],
    *,
    known_commands: set[str],
    global_harness: str | None = None,
) -> None:
    candidate = _first_positional_token(argv)
    if candidate is None:
        return
    if candidate in known_commands:
        return
    if global_harness is not None:
        return
    print(f"error: Unknown command: {candidate}", file=sys.stderr)
    raise SystemExit(1)


def is_root_help_request(argv: Sequence[str]) -> bool:
    if not any(token in {"--help", "-h"} for token in argv):
        return False
    return _first_positional_token(argv) is None


def _state_requirement_for_argv(argv: Sequence[str]) -> StateRequirement | None:
    """Return catalog startup state policy for argv without importing handlers."""

    from meridian.cli.startup.catalog import COMMAND_CATALOG
    from meridian.cli.startup.classify import classify_invocation

    descriptor = classify_invocation(argv, COMMAND_CATALOG)
    if descriptor is None:
        return None
    return descriptor.state_requirement


def maybe_bootstrap_runtime_state(
    argv: Sequence[str],
    *,
    agent_mode: bool,
    state_requirement: StateRequirement | None = None,
) -> Path | None:
    """Prepare startup state according to catalog policy and return project root.

    The return value lets the CLI reuse the first project-root resolution for
    downstream startup work instead of resolving it again.
    """

    if agent_mode:
        return None
    try:
        from meridian.lib.bootstrap.services import (
            prepare_for_project_read,
            prepare_for_project_write,
            prepare_for_runtime_read,
            prepare_for_runtime_write,
        )
        from meridian.lib.config.project_root import resolve_project_root

        requirement = state_requirement or _state_requirement_for_argv(argv)
        if requirement in {None, StateRequirement.NONE}:
            return None

        project_root = resolve_project_root()

        if requirement == StateRequirement.PROJECT_READ:
            prepare_for_project_read(project_root)
        elif requirement == StateRequirement.RUNTIME_READ:
            prepare_for_runtime_read(project_root)
        elif requirement == StateRequirement.PROJECT_WRITE:
            prepare_for_project_write(project_root)
        elif requirement == StateRequirement.RUNTIME_WRITE:
            prepare_for_runtime_write(project_root)

        return project_root
    except Exception:
        return None


@contextmanager
def temporary_config_env(config_file: str | None) -> Iterator[None]:
    if config_file is None:
        yield
        return

    prior_user_config = os.environ.get("MERIDIAN_CONFIG")
    os.environ["MERIDIAN_CONFIG"] = config_file
    try:
        yield
    finally:
        if prior_user_config is None:
            os.environ.pop("MERIDIAN_CONFIG", None)
        else:
            os.environ["MERIDIAN_CONFIG"] = prior_user_config
