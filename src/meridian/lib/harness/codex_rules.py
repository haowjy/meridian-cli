from __future__ import annotations

import json
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from meridian.lib.harness.codex_rollout import resolve_codex_home
from meridian.lib.launch.launch_types import CodexRuntimeResolution
from meridian.lib.launch.permissions import (
    INBOX_PLATFORM_COMMAND,
    MESSAGE_PLATFORM_COMMAND,
    SPAWN_PLATFORM_COMMAND,
)
from meridian.lib.platform.atomic import AtomicReplaceDurabilityError, atomic_write_text
from meridian.lib.safety.policy import CONSTRAINED_SANDBOX_MODES, PermissionPolicy


def _rules_content(command: str) -> str:
    return "".join(
        (
            "# This file is owned and rewritten by Meridian.\n",
            f'prefix_rule(pattern={json.dumps(shlex.split(command))}, decision="allow")\n',
        )
    )


@dataclass(frozen=True)
class RulesFile:
    name: str
    command: str

    @property
    def content(self) -> str:
        return _rules_content(self.command)


MERIDIAN_RULES_FILE_CONTENT = _rules_content(SPAWN_PLATFORM_COMMAND.command)

# A file's content never changes once a release has written it: a build rewrites a file
# whose bytes differ from its own, so two builds disagreeing about one file swap it back
# and forth, and the older build never writes a file it doesn't know. A new grant is a
# new file.
RULES_FILES: tuple[RulesFile, ...] = (
    RulesFile("meridian.rules", SPAWN_PLATFORM_COMMAND.command),
    RulesFile("meridian-message.rules", MESSAGE_PLATFORM_COMMAND.command),
    RulesFile("meridian-inbox.rules", INBOX_PLATFORM_COMMAND.command),
)


def codex_rules_files(policy: PermissionPolicy) -> tuple[RulesFile, ...]:
    """The rules files a launch writes: one per command it may run outside its sandbox.

    Codex has no per-launch rule, so only a sandbox Meridian constrains needs them, and a
    launch writes only the grants it was given.
    """

    if policy.sandbox not in CONSTRAINED_SANDBOX_MODES:
        return ()
    granted = {command.command for command in policy.outside_sandbox_commands}
    return tuple(file for file in RULES_FILES if file.command in granted)


def _and_join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def escaping_commands_text(files: tuple[RulesFile, ...]) -> str:
    """Name the files' commands for the lines that tell a user what the files do."""

    return _and_join([f"`{file.command}`" for file in files])


def rules_files_text(files: tuple[RulesFile, ...]) -> str:
    """Name the files for the lines that tell a user what the files do."""

    return _and_join([file.name for file in files])


def _ensure_rules_file(rules_dir: Path, file: RulesFile) -> str | None:
    """Write one file when its bytes differ; the error text when it can't be written."""

    path = rules_dir / file.name
    try:
        try:
            if path.read_bytes() == file.content.encode():
                return None
        except FileNotFoundError:
            pass
        atomic_write_text(path, file.content)
    except AtomicReplaceDurabilityError:
        # The rename committed. A directory fsync failure weakens crash durability but
        # does not leave this launch without the rule it is about to use.
        return None
    except OSError as error:
        return str(error)
    return None


def ensure_codex_meridian_rules(
    launch_env: Mapping[str, str],
    *,
    child_cwd: Path,
    files: tuple[RulesFile, ...],
) -> CodexRuntimeResolution:
    """Atomically project ``files`` into the launch environment's Codex home.

    ``rules_file_path`` names the first of them; ``rules_file_error`` is the first failure
    among them.
    """

    codex_home = resolve_codex_home(launch_env)
    if not codex_home.is_absolute():
        codex_home = child_cwd / codex_home
    try:
        resolved_home = codex_home.resolve()
    except OSError as error:
        return CodexRuntimeResolution(
            rules_file_path=(codex_home / "rules" / files[0].name).as_posix(),
            rules_file_error=str(error),
        )
    rules_dir = resolved_home / "rules"
    errors = [error for file in files if (error := _ensure_rules_file(rules_dir, file)) is not None]
    return CodexRuntimeResolution(
        rules_file_path=(rules_dir / files[0].name).as_posix(),
        rules_file_error=errors[0] if errors else None,
    )


__all__ = [
    "MERIDIAN_RULES_FILE_CONTENT",
    "RULES_FILES",
    "codex_rules_files",
    "ensure_codex_meridian_rules",
    "escaping_commands_text",
    "rules_files_text",
]
