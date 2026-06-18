"""Bootstrap helpers for meridian CLI startup behavior."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from meridian.cli.mode import RenderMode, is_agent_render_mode, parse_render_mode
from meridian.cli.startup.policy import StateRequirement

# Keep these startup parse tables in sync with `@app.default root(...)` in
# `main.py` plus startup-only flags parsed before cyclopts (`--verbose` at root).
_TOP_LEVEL_VALUE_FLAGS = frozenset(
    {
        "--format",
        "--config",
        "--continue",
        "--fork",
        "--fork-fresh",
        "--from",
        "--file",
        "-f",
        "--prompt-file",
        "--prompt",
        "-p",
        "--skills",
        "--goal",
        "--model",
        "-m",
        "--harness",
        "-a",
        "--agent",
        "--work",
        "--task-dir",
        "--autocompact",
        "--autocompact-pct",
        "--effort",
        "--sandbox",
        "--approval",
        "--timeout",
        "-C",
        "--directory",
        "--mode",
    }
)
_TOP_LEVEL_BOOL_FLAGS = frozenset(
    {
        "--help",
        "-h",
        "--version",
        "-v",
        "--verbose",
        "--json",
        "--no-json",
        "--yes",
        "--no-yes",
        "--no-input",
        "--no-no-input",
        "--yolo",
        "--no-yolo",
        "--dry-run",
        "--no-dry-run",
    }
)
HARNESS_SHORTCUT_NAMES = frozenset({"claude", "codex", "cursor", "opencode", "pi"})
_CHAT_MANAGEMENT_SUBCOMMANDS = frozenset({"ls", "show", "log", "close"})


@dataclass(frozen=True)
class ParsedGlobalOptions:
    output_format: str
    config_file: str | None
    harness: str | None
    yes: bool
    no_input: bool
    output_explicit: bool
    forced_render_mode: RenderMode | None
    directory: str | None
    directory_explicit: bool = False


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
    directory: str | None = None
    yes = False
    no_input = False
    output_explicit = False
    forced_render_mode: RenderMode | None = None
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
        if arg in {"-C", "--directory"}:
            if index + 1 >= len(argv):
                raise SystemExit(f"{arg} requires a value")
            directory = argv[index + 1].strip()
            if not directory:
                raise SystemExit(f"{arg} requires a non-empty value")
            index += 2
            continue
        if arg.startswith("--directory="):
            directory = arg.partition("=")[2].strip()
            if not directory:
                raise SystemExit("--directory requires a non-empty value")
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
        if arg == "--mode":
            if index + 1 >= len(argv):
                raise SystemExit("--mode requires a value")
            parsed_mode = parse_render_mode(argv[index + 1])
            if forced_render_mode is not None and parsed_mode != forced_render_mode:
                raise SystemExit("Cannot combine conflicting --mode values.")
            forced_render_mode = parsed_mode
            index += 2
            continue
        if arg.startswith("--mode="):
            parsed_mode = parse_render_mode(arg.partition("=")[2])
            if forced_render_mode is not None and parsed_mode != forced_render_mode:
                raise SystemExit("Cannot combine conflicting --mode values.")
            forced_render_mode = parsed_mode
            index += 1
            continue
        if arg == "--verbose" and _first_positional_token(cleaned) is None:
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
                    f"Conflicting harness selections: '{harness}' and '{shortcut_value}'."
                )
            harness = shortcut_value
            harness_source = shortcut_value
            del cleaned[shortcut_index]

    if harness is not None and _is_chat_management_invocation(cleaned):
        if harness_source == "--harness":
            raise SystemExit('Unknown option: "--harness"')
        if harness_source in HARNESS_SHORTCUT_NAMES:
            raise SystemExit(f'Unknown option: "{harness_source}"')

    return cleaned, ParsedGlobalOptions(
        output_format=normalize_output_format(output_format, json_mode),
        config_file=config_file,
        harness=harness,
        directory=directory,
        directory_explicit=directory is not None,
        yes=yes,
        no_input=no_input,
        output_explicit=output_explicit,
        forced_render_mode=forced_render_mode,
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
    render_mode: RenderMode,
    state_requirement: StateRequirement | None = None,
) -> Path | None:
    """Prepare startup state according to catalog policy and return project root.

    The return value lets the CLI reuse the first project-root resolution for
    downstream startup work instead of resolving it again.
    """

    if is_agent_render_mode(render_mode):
        return None
    try:
        from meridian.cli.utils import require_established_project_root
        from meridian.lib.bootstrap.services import (
            prepare_for_project_read,
            prepare_for_project_write,
            prepare_for_runtime_read,
            prepare_for_runtime_write,
        )

        requirement = state_requirement or _state_requirement_for_argv(argv)
        if requirement in {None, StateRequirement.NONE}:
            return None

        project_root = require_established_project_root()

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
def meridian_managed_env_default() -> Generator[None, None, None]:
    prior = os.environ.get("MERIDIAN_MANAGED")
    os.environ.setdefault("MERIDIAN_MANAGED", "1")
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop("MERIDIAN_MANAGED", None)
        else:
            os.environ["MERIDIAN_MANAGED"] = prior


@contextmanager
def temporary_config_env(config_file: str | None) -> Generator[None, None, None]:
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
