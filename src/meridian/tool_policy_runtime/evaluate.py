"""The one evaluator of a launch's tool rules; stdlib only, run as a script per tool call.

Pi's ``tool_call`` extension and Codex's ``PreToolUse`` hook both ask it, so a rule means the
same on both (``tests/fixtures/tool_policy_cases.json`` is its decision table). It must not
import Meridian: Codex starts it once per shell call (see #721 for what a CLI import costs).
It is a guard against accidents such as a careless ``git reset --hard``, not a sandbox.

Request, on stdin: ``{"tool_name", "tool_input", "cwd"}`` (Codex's own payload shape; Codex's
``Bash`` is ``bash``). Policy: ``tool_extension_policy.ExtensionPolicy`` as JSON in
``_MERIDIAN_TOOL_POLICY``; a missing variable refuses. Answer, by caller:

* default (Codex): exit 0 to let the call run; exit 2 with the reason on stderr to refuse it.
  Codex cannot ask, so ``ask`` refuses. Any other exit is turned into a refusal by the hook
  command's wrapper (``codex_tool_hook.hook_command``).
* ``--json`` (Pi): exit 0 and one JSON object on stdout, ``{"action", "detail", "reason"}``,
  ``action`` being ``allow``, ``ask`` or ``deny``. ``reason`` is what the model is told when
  the call is refused (for ``ask``, when nobody can be asked); ``detail`` is what a prompt
  shows. Anything else, including a crash, is the caller's to refuse.

The rules that match a call are decided by the strictest, however narrow its key; the ``*``
rules decide only when none matches. A bash command is split on ``;``, ``&&``, ``||``, ``|``,
``&`` and newlines (outside quotes) and each segment is matched; the strictest segment wins.
With ``bash_fail_closed`` the command must also be simple (no expansion, redirection,
backgrounding), else it is refused. What the matcher does not see is not a bug: wrappers
(``sudo git reset --hard``), ``bash -c`` strings, substitutions, subshells, options between a
program and its subcommand, scripts and interpreters. Do not add a shell parser to close
individual gaps. Every step is linear in the input.
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
# Shell syntax outside quotes that an allowlist will not vouch for.
_STRICT_UNQUOTED = frozenset("\\$`()<>#{}\r")
_STRICT_IN_DOUBLE = frozenset("$`\\")
_PATH_TOOLS = frozenset({"read", "edit", "write", "grep", "find", "ls"})
_RANK = {"allow": 0, "ask": 1, "deny": 2}


@dataclass(frozen=True)
class Decision:
    action: str
    detail: str
    reason: str


@dataclass(frozen=True)
class Rule:
    tool: str
    pattern: str | None
    action: str
    label: str


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

    skip = False
    for index, ch in enumerate(src):
        if skip:
            skip = False
            continue
        if quote:
            if ch == quote:
                quote = ""
            elif strict and quote == '"' and ch in _STRICT_IN_DOUBLE:
                return None
        elif ch in "'\"":
            quote = ch
        elif ch in _SEPARATORS:
            if ch == "&" and src[index + 1 : index + 2] == "&":
                skip = True
            elif ch == "&" and strict:
                return None
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


def _anchor(path: str, cwd: str) -> str:
    return path if os.path.isabs(path) else os.path.join(os.path.abspath(cwd), path)


def _scope_path(path: str, cwd: str) -> str:
    """The path with symlinks and ``..`` resolved as far as it exists, so a link cannot carry a
    scoped allow outside its root."""

    return os.path.realpath(_anchor(path, cwd))


def _pattern_path(pattern: str, cwd: str) -> str:
    """A path rule's pattern with the part before its first ``*`` resolved like a scope."""

    absolute = _anchor(pattern, cwd)
    wildcard = absolute.find("*")
    if wildcard < 0:
        return os.path.realpath(absolute)
    boundary = max(absolute.rfind(os.sep, 0, wildcard), 0)
    return os.path.realpath(absolute[:boundary] or os.sep).rstrip(os.sep) + absolute[boundary:]


def _scope(tool: str, tool_input: dict[str, object]) -> str | None:
    """What a rule's pattern is matched against, or None if the call has none to read."""

    if tool == "bash":
        value = tool_input.get("command")
    elif tool in {"read", "edit", "write"}:
        value = tool_input.get("path")
    elif tool in {"grep", "find", "ls"}:
        value = tool_input.get("path", ".")
    else:
        return None
    return value if isinstance(value, str) else None


def _rules(policy: dict[str, object]) -> list[Rule]:
    rules: list[Rule] = []
    for raw in cast("list[dict[str, object]]", policy["rules"]):
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


def _decide_scope(
    rules: list[Rule], tool: str, scope: str | None, detail: str, cwd: str
) -> Decision | None:
    own = [rule for rule in rules if rule.tool == tool]
    if scope is None and any(rule.pattern is not None for rule in own):
        return Decision(
            "deny", detail, f"{DENIAL_PREFIX}the scope for {tool} could not be evaluated."
        )
    matching: list[Rule] = []
    for rule in own:
        if rule.pattern is None:
            matching.append(rule)
        elif scope is not None:
            pattern = _pattern_path(rule.pattern, cwd) if tool in _PATH_TOOLS else rule.pattern
            if _glob_matches(pattern, scope):
                matching.append(rule)
    winner: Rule | None = None
    for rule in matching or [rule for rule in rules if rule.tool == "*"]:
        if winner is None or _RANK[rule.action] > _RANK[winner.action]:
            winner = rule
    if winner is None:
        return None
    return Decision(
        winner.action,
        detail,
        f"{DENIAL_PREFIX}{winner.label} = {winner.action}. Attempted: {detail}",
    )


def _granted(grant: dict[str, object], text: str) -> bool:
    command = str(grant["command"])
    if grant["match"] == "exact":
        return text == command
    return text == command or text.startswith(f"{command} ")


def decide(
    policy: dict[str, object], tool: str, tool_input: dict[str, object], cwd: str
) -> Decision | None:
    """How the policy answers a call, or None when no rule has an opinion (the call runs)."""

    decision = _decide(policy, tool, tool_input, cwd)
    if decision is not None and decision.action == "ask":
        return Decision("ask", decision.detail, str(policy["headless_reject_reason"]))
    return decision


def _decide(
    policy: dict[str, object], tool: str, tool_input: dict[str, object], cwd: str
) -> Decision | None:
    rules = _rules(policy)
    scope = _scope(tool, tool_input)
    detail = scope if scope is not None else tool
    if tool != "bash":
        if scope is not None and tool in _PATH_TOOLS:
            scope = _scope_path(scope, cwd)
        return _decide_scope(rules, tool, scope, detail, cwd)

    strict = bool(policy["bash_fail_closed"])
    if scope is None:
        if not strict:
            return None
        return Decision("deny", detail, f"{DENIAL_PREFIX}the bash command could not be evaluated.")
    segments = split_segments(scope, strict=strict)
    if segments is None or not segments:
        if not strict:
            return None
        return Decision(
            "deny",
            detail,
            f"{DENIAL_PREFIX}restricted bash accepts only simple commands; run simple commands "
            "without expansion, redirection, or backgrounding.",
        )
    # Meridian's own commands are granted only as a whole command, before any rule is read.
    if len(segments) == 1:
        grants = cast("list[dict[str, object]]", policy["meridian_commands"])
        if any(_granted(grant, segments[0]) for grant in grants):
            return Decision("allow", detail, "Meridian command")
    decisions = [_decide_scope(rules, tool, segment, detail, cwd) for segment in segments]
    return max(
        (decision for decision in decisions if decision is not None),
        key=lambda decision: _RANK[decision.action],
        default=None,
    )


def _request(raw: str) -> tuple[str, dict[str, object], str]:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("request is not an object")
    data = cast("dict[str, object]", payload)
    name = data.get("tool_name")
    tool_input = data.get("tool_input")
    cwd = data.get("cwd")
    if not isinstance(name, str) or not isinstance(tool_input, dict):
        raise ValueError("request needs tool_name and tool_input")
    return (
        "bash" if name == "Bash" else name,
        cast("dict[str, object]", tool_input),
        cwd if isinstance(cwd, str) and cwd else os.getcwd(),
    )


def main(argv: list[str]) -> int:
    raw_policy = os.environ.get(POLICY_ENV)
    if not raw_policy:
        print(
            f"{DENIAL_PREFIX}{POLICY_ENV} is missing, so the tool policy cannot be read.",
            file=sys.stderr,
        )
        return 2
    tool, tool_input, cwd = _request(sys.stdin.read())
    decision = decide(cast("dict[str, object]", json.loads(raw_policy)), tool, tool_input, cwd)
    if "--json" in argv:
        answer = decision or Decision("allow", "", "")
        print(
            json.dumps({"action": answer.action, "detail": answer.detail, "reason": answer.reason})
        )
        return 0
    if decision is None or decision.action == "allow":
        return 0
    print(decision.reason, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
