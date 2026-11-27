from __future__ import annotations

import json
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from meridian.lib.harness.codex_rollout import resolve_codex_home
from meridian.lib.launch.launch_types import CodexRuntimeResolution
from meridian.lib.launch.permissions import MESSAGE_PLATFORM_COMMAND, SPAWN_PLATFORM_COMMAND
from meridian.lib.platform.atomic import AtomicReplaceDurabilityError, atomic_write_text


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
    content: str


MERIDIAN_RULES_FILE_CONTENT = _rules_content(SPAWN_PLATFORM_COMMAND.command)

# A file's content never changes once a release has written it: a build rewrites a file
# whose bytes differ from its own, so two builds disagreeing about one file swap it back
# and forth, and the older build never writes a file it doesn't know. A new grant is a
# new file.
RULES_FILES: tuple[RulesFile, ...] = (
    RulesFile("meridian.rules", MERIDIAN_RULES_FILE_CONTENT),
    RulesFile("meridian-message.rules", _rules_content(MESSAGE_PLATFORM_COMMAND.command)),
)


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
) -> CodexRuntimeResolution:
    """Atomically project Meridian's rules into the launch environment's Codex home.

    ``rules_file_path`` names ``meridian.rules``; ``rules_file_error`` is the first
    failure among the files.
    """

    codex_home = resolve_codex_home(launch_env)
    if not codex_home.is_absolute():
        codex_home = child_cwd / codex_home
    try:
        resolved_home = codex_home.resolve()
    except OSError as error:
        return CodexRuntimeResolution(
            rules_file_path=(codex_home / "rules" / RULES_FILES[0].name).as_posix(),
            rules_file_error=str(error),
        )
    rules_dir = resolved_home / "rules"
    errors = [
        error for file in RULES_FILES if (error := _ensure_rules_file(rules_dir, file)) is not None
    ]
    return CodexRuntimeResolution(
        rules_file_path=(rules_dir / RULES_FILES[0].name).as_posix(),
        rules_file_error=errors[0] if errors else None,
    )


__all__ = ["MERIDIAN_RULES_FILE_CONTENT", "RULES_FILES", "ensure_codex_meridian_rules"]
