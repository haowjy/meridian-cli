"""The ``PreToolUse`` hook Meridian puts on a Codex app-server to enforce scoped shell rules.

The hook definition and its trust both ride the app-server's ``-c`` flags, so Meridian
writes no ``config.toml`` and a shared ``CODEX_HOME`` stays untouched. A hook with no trust
record is skipped without a warning, and Codex fails open on every hook failure, so two
guards keep it from silently not running: the command turns any failure of the enforcer into
a block, and ``CodexConnection.start`` calls ``hooks/list``
and refuses the launch unless Codex reports exactly this hook, trusted (``check_hooks_list``).

``currentHash`` is the SHA-256 of the hook's identity as Codex serialises it (sorted keys,
compact separators); a Codex that changes the format reports the hook ``modified``, which the
launch check turns into a refusal.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import sys
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import cast

from meridian.lib.harness.projections.native_config import codex_toml_literal

_ENFORCER = Path(__file__).resolve().parents[2] / "codex_runtime" / "tool_hook.py"
# Where Codex files a hook that arrived as a `-c` flag.
_HOOK_KEY = "/<session-flags>/config.toml:pre_tool_use:0:0"
_MATCHER = "Bash"
# The enforcer is linear in the command; Codex fails open on a timeout, so only a stuck
# process reaches this.
_TIMEOUT_SECONDS = 10


@cache
def hook_command() -> str:
    """Run the enforcer with the launching interpreter, and block if it cannot decide.

    ``-I`` keeps an agent-writable environment from steering its imports. The enforcer exits
    2 to deny with its own reason; any other failure (missing interpreter or script, a crash)
    gets a message here, because Codex otherwise lets the call run.
    """

    run = f"{shlex.quote(sys.executable)} -I {shlex.quote(str(_ENFORCER))}"
    message = "Blocked by Meridian policy: the shell policy hook failed to run; restart the launch."
    return f"{run} || {{ [ $? -eq 2 ] || echo {shlex.quote(message)} >&2; exit 2; }}"


def _hook_hash(command: str) -> str:
    identity = {
        "event_name": "pre_tool_use",
        "hooks": [
            {"async": False, "command": command, "timeout": _TIMEOUT_SECONDS, "type": "command"}
        ],
        "matcher": _MATCHER,
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def hook_config_args() -> tuple[str, ...]:
    """The app-server ``-c`` flags that define the hook and record its trust.

    The state table must be an inline table: the dotted spelling is ignored.
    """

    command = hook_command()
    definition = [
        {
            "matcher": _MATCHER,
            "hooks": [{"type": "command", "command": command, "timeout": _TIMEOUT_SECONDS}],
        }
    ]
    state = {_HOOK_KEY: {"trusted_hash": _hook_hash(command)}}
    return (
        "-c",
        f"hooks.PreToolUse={codex_toml_literal(definition)}",
        "-c",
        f"hooks.state={codex_toml_literal(state)}",
    )


def check_hooks_list(result: Mapping[str, object]) -> str | None:
    """Why a ``hooks/list`` result does not show Meridian's hook as running, or None if it does."""

    expected = _hook_hash(hook_command())
    matching = 0
    for entry in cast("list[Mapping[str, object]]", result.get("data") or []):
        for hook in cast("list[Mapping[str, object]]", entry.get("hooks") or []):
            if (
                hook.get("source") == "sessionFlags"
                and hook.get("eventName") == "preToolUse"
                and hook.get("trustStatus") == "trusted"
                and hook.get("enabled") is True
                and hook.get("currentHash") == expected
            ):
                matching += 1
    if matching == 1:
        return None
    return (
        f"expected exactly one trusted Meridian PreToolUse hook, Codex listed {matching}; "
        "a passthrough `-c hooks...` or `--disable hooks`, or a Codex that hashes hooks "
        "differently, turned it off"
    )
