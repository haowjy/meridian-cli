from __future__ import annotations

import json
import shlex
from collections.abc import Mapping
from pathlib import Path

from meridian.lib.harness.codex_rollout import resolve_codex_home
from meridian.lib.launch.launch_types import CodexRuntimeResolution, ResolvedLaunchSpec
from meridian.lib.launch.permissions import SPAWN_PLATFORM_COMMAND
from meridian.lib.platform.atomic import AtomicReplaceDurabilityError, atomic_write_text
from meridian.lib.safety.policy import CONSTRAINED_SANDBOX_MODES


def _rule_content(command: str) -> str:
    return (
        "# This file is owned and rewritten by Meridian.\n"
        f"prefix_rule(pattern={json.dumps(shlex.split(command))}, "
        'decision="allow")\n'
    )


MERIDIAN_RULES_FILE_CONTENT = _rule_content(SPAWN_PLATFORM_COMMAND.command)


def codex_meridian_command(spec: ResolvedLaunchSpec) -> str | None:
    policy = spec.permission_policy
    if policy.sandbox in CONSTRAINED_SANDBOX_MODES and policy.has_spawn_capability:
        return SPAWN_PLATFORM_COMMAND.command
    if spec.agent_name is None and policy.sandbox != "danger-full-access":
        return "meridian spawn message"
    return None


def ensure_codex_meridian_rules(
    spec: ResolvedLaunchSpec,
    launch_env: Mapping[str, str],
    *,
    child_cwd: Path,
) -> CodexRuntimeResolution:
    """Install the rule in the Codex home selected for this launch."""
    command = codex_meridian_command(spec)
    if command is None:
        return CodexRuntimeResolution()
    if command == SPAWN_PLATFORM_COMMAND.command:
        filename, content = "meridian.rules", MERIDIAN_RULES_FILE_CONTENT
    else:
        # Concurrent launches can share this Codex home and need the broader rule.
        filename, content = "meridian-message.rules", _rule_content(command)
    codex_home = resolve_codex_home(launch_env)
    if not codex_home.is_absolute():
        codex_home = child_cwd / codex_home
    try:
        resolved_home = codex_home.resolve()
    except OSError as error:
        return CodexRuntimeResolution(
            rules_file_path=(codex_home / "rules" / filename).as_posix(),
            rules_file_error=str(error),
        )
    rules_path = resolved_home / "rules" / filename
    try:
        try:
            if rules_path.read_bytes() == content.encode():
                return CodexRuntimeResolution(rules_file_path=rules_path.as_posix())
        except FileNotFoundError:
            pass
        atomic_write_text(rules_path, content)
    except AtomicReplaceDurabilityError:
        # The rename committed. A directory fsync failure weakens crash durability but
        # does not leave this launch without the rule it is about to use.
        return CodexRuntimeResolution(rules_file_path=rules_path.as_posix())
    except OSError as error:
        return CodexRuntimeResolution(
            rules_file_path=rules_path.as_posix(),
            rules_file_error=str(error),
        )
    return CodexRuntimeResolution(rules_file_path=rules_path.as_posix())


__all__ = ["MERIDIAN_RULES_FILE_CONTENT", "codex_meridian_command", "ensure_codex_meridian_rules"]
