"""Codex ``PreToolUse`` hook: refuse shell commands that match a launch's deny rules; stdlib only.

Codex runs this once per shell call, so it must not import Meridian (see #721 for what a CLI
import costs). It is a guard against accidents such as a careless ``git reset --hard``, not a
sandbox: an agent that tries can get past it. The rule matching mirrors the Pi permission
extension's ``policyDecision`` (both run ``tests/fixtures/tool_policy_cases.json``), so a rule
means the same on both, and the policy JSON is ``tool_extension_policy.ExtensionPolicy``.

The command is split on ``;``, ``&&``, ``||``, ``|``, ``&`` and newlines (outside quotes) and
each segment is wildcard-matched against the rules. Of the rules that match, deny wins; a
segment no bash rule matches is decided by the ``*`` rule, else it passes; an allowlist
(``bash_fail_closed``) also refuses ``$``, backticks, redirection and other shell syntax as Pi
does. What the matcher does not see is not a bug: wrappers (``sudo git reset --hard``),
``bash -c`` strings, substitutions, subshells, options between a program and its subcommand,
scripts and interpreters.

The policy arrives in ``_MERIDIAN_TOOL_POLICY``; a missing variable denies. Exit 2 with the
reason on stderr is the block; any other failure exit is turned into a block by the hook
command's own wrapper (``codex_tool_hook.hook_command``). Every step is linear in the command.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import cast

POLICY_ENV = "_MERIDIAN_TOOL_POLICY"
DENIAL_PREFIX = "Blocked by Meridian policy: "
_SEPARATORS = frozenset(";|&\n")
# Pi's simple-command check: shell syntax outside quotes that an allowlist will not vouch for.
_STRICT_UNQUOTED = frozenset("\\$`()<>#{}\r")
_STRICT_IN_DOUBLE = frozenset("$`\\")


def split_segments(src: str, *, strict: bool) -> list[str] | None:
    """The command's segments, or None if ``strict`` and it holds syntax an allowlist refuses."""

    segments: list[str] = []
    current: list[str] = []
    quote = ""

    def flush() -> None:
        text = "".join(current).strip()
        if text:
            segments.append(text)
        current.clear()

    for ch in src:
        if quote:
            if ch == quote:
                quote = ""
            elif strict and quote == '"' and ch in _STRICT_IN_DOUBLE:
                return None
        elif ch in "'\"":
            quote = ch
        elif ch in _SEPARATORS:
            flush()
            continue
        elif strict and ch in _STRICT_UNQUOTED:
            return None
        current.append(ch)
    flush()
    return None if strict and (quote or not segments) else segments


def _glob_matches(pattern: str, value: str) -> bool:
    """``*`` matches any run of characters. Linear in the value: no input makes it slow."""

    if pattern.endswith(" *") and value == pattern[:-2]:
        return True
    first, *middle = pattern.split("*")
    if not middle:
        return value == first
    last = middle.pop()
    end = len(value) - len(last)
    if end < len(first) or not value.startswith(first) or not value.endswith(last):
        return False
    position = len(first)
    for part in middle:
        found = value.find(part, position, end)
        if found < 0:
            return False
        position = found + len(part)
    return True


_ACTION_RANK = {"allow": 0, "deny": 2}


@dataclass(frozen=True)
class Rule:
    tool: str
    pattern: str | None
    action: str
    label: str


def _winner(rules: list[Rule], command: str) -> Rule | None:
    """The rule deciding ``command``: the strictest of the bash rules that match it, however
    narrow its key; the ``*`` rules decide only when none matches."""

    matching = [
        rule
        for rule in rules
        if rule.tool == "bash" and (rule.pattern is None or _glob_matches(rule.pattern, command))
    ]
    deciding = matching or [rule for rule in rules if rule.tool == "*"]
    best: Rule | None = None
    for rule in deciding:
        if best is None or _ACTION_RANK[rule.action] > _ACTION_RANK[best.action]:
            best = rule
    return best


def _granted(grant: dict[str, object], text: str) -> bool:
    command = str(grant["command"])
    if grant["match"] == "exact":
        return text == command
    return text == command or text.startswith(f"{command} ")


def _rules(policy: dict[str, object]) -> list[Rule]:
    """The policy's shell rules. The compile has already turned Codex's unanswerable ``ask``
    into ``deny``."""

    rules: list[Rule] = []
    for raw in cast("list[dict[str, object]]", policy["rules"]):
        if raw["tool"] not in {"bash", "*"}:
            continue
        pattern = raw.get("pattern")
        rules.append(
            Rule(
                str(raw["tool"]),
                pattern if isinstance(pattern, str) else None,
                str(raw["action"]),
                str(raw["label"]),
            )
        )
    return rules


def _denial(rule: Rule, command: str) -> str:
    return f"{DENIAL_PREFIX}{rule.label} = {rule.action}. Attempted: {command}"


def decide(policy: dict[str, object], command: str) -> str | None:
    """The reason ``command`` is denied under ``policy``, or None to let it run."""

    strict = bool(policy["bash_fail_closed"])
    segments = split_segments(command, strict=strict)
    if segments is None:
        return (
            f"{DENIAL_PREFIX}restricted bash accepts only simple commands; run simple commands "
            "without expansion, redirection, or backgrounding."
        )
    # Meridian's own commands are granted only as a whole command, before any rule is read,
    # as Pi grants them.
    if len(segments) == 1:
        grants = cast("list[dict[str, object]]", policy["meridian_commands"])
        if any(_granted(grant, segments[0]) for grant in grants):
            return None
    rules = _rules(policy)
    for segment in segments:
        winner = _winner(rules, segment)
        if winner is not None and winner.action == "deny":
            return _denial(winner, command)
    return None


def _payload_command(raw: str) -> str | None:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        return None
    data = cast("dict[str, object]", payload)
    if data.get("tool_name") != "Bash":
        return None
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        raise ValueError("Bash call without tool_input")
    command = cast("dict[str, object]", tool_input).get("command")
    if not isinstance(command, str):
        raise ValueError("Bash call without a command string")
    return command


def main() -> int:
    raw_policy = os.environ.get(POLICY_ENV)
    if not raw_policy:
        print(
            f"{DENIAL_PREFIX}{POLICY_ENV} is missing, so the shell policy cannot be read.",
            file=sys.stderr,
        )
        return 2
    command = _payload_command(sys.stdin.read())
    if command is None:
        return 0
    reason = decide(cast("dict[str, object]", json.loads(raw_policy)), command)
    if reason is None:
        return 0
    print(reason, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
