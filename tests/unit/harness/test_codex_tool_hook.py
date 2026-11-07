"""The Codex PreToolUse enforcer's decision table: its wildcard segment matching."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

import meridian


def _load_enforcer() -> ModuleType:
    path = Path(meridian.__file__).parent / "codex_runtime" / "tool_hook.py"
    spec = importlib.util.spec_from_file_location("codex_tool_hook_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through it
    spec.loader.exec_module(module)
    return module


_hook = _load_enforcer()


def _rule(pattern: str | None, action: str, tool: str = "bash") -> dict[str, object]:
    return {
        "capability": tool,
        "tool": tool,
        "pattern": pattern,
        "action": action,
        "label": f"bash({pattern})" if pattern else "bash outside its scoped allows",
    }


_DENY_LIST = {
    "rules": [_rule("git reset --hard *", "deny")],
    "meridian_commands": [],
    "bash_fail_closed": False,
}
_ALLOWLIST = {
    "rules": [_rule(None, "deny"), *(_rule(p, "allow") for p in ("git *", "rg *"))],
    "meridian_commands": [
        {"command": "meridian spawn done", "match": "prefix", "role": "completion"},
        {"command": "meridian work show", "match": "prefix", "role": "read"},
    ],
    "bash_fail_closed": True,
}
# `"*": deny` beside a scoped bash allow, and a bash rule that outranks `*`.
_STAR_DENY = {
    "rules": [_rule(None, "deny", "*"), _rule("git *", "allow")],
    "meridian_commands": [],
    "bash_fail_closed": True,
}
_BASH_OVER_STAR = {
    "rules": [_rule(None, "deny", "*"), _rule(None, "allow"), _rule("rm *", "deny")],
    "meridian_commands": [],
    "bash_fail_closed": False,
}

_DENIED_BY_LIST = [
    "git reset --hard",
    "git reset --hard HEAD~1",
    "cd x && git reset --hard",
    "git status; git reset --hard",
    "git status || git reset --hard",
    "git status | git reset --hard",
    "sleep 1 & git reset --hard",
    "git status\ngit reset --hard",
    "echo 'a;b' && git reset --hard",
]
# The matcher does not see wrappers, quoted-away separators, substitutions, or options
# between a program and its subcommand.
_ALLOWED_BY_LIST = [
    "git status",
    "git reset --soft HEAD",
    "echo 'git reset --hard'",
    "echo 'a && git reset --hard'",
    "sudo git reset --hard",
    "bash -c 'git reset --hard'",
    "echo $(git reset --hard)",
    "git -C . reset --hard",
]
_DENIED_BY_ALLOWLIST = [
    "touch x",
    "git status && touch x",
    "git status | touch x",
    "git status; touch x",
    "git status\ntouch x",
    "meridian work list",
    "meridian spawn done && touch x",
    "git status > out",
    "git status 2>&1",
    "git $(touch x)",
    "git `touch x`",
    'git "$HOME"',
    "git 'unterminated",
    "git status #",
    "",
]
_ALLOWED_BY_ALLOWLIST = [
    "git status",
    "git status && rg x | git log",
    "git commit -m 'a; b'",
    "meridian spawn done",
    "meridian work show foo",
]


@pytest.mark.parametrize(
    ("policy", "command", "denied"),
    [
        *((_DENY_LIST, command, True) for command in _DENIED_BY_LIST),
        *((_DENY_LIST, command, False) for command in _ALLOWED_BY_LIST),
        *((_ALLOWLIST, command, True) for command in _DENIED_BY_ALLOWLIST),
        *((_ALLOWLIST, command, False) for command in _ALLOWED_BY_ALLOWLIST),
        *((_STAR_DENY, command, True) for command in ("touch x", "git status; touch x")),
        *((_STAR_DENY, command, False) for command in ("git status", "git log | git show")),
        *((_BASH_OVER_STAR, command, True) for command in ("rm -rf x", "git status; rm x")),
        *((_BASH_OVER_STAR, command, False) for command in ("touch x", "git status")),
        # A pattern with many interior stars must not make the match slow.
        (
            {**_DENY_LIST, "rules": [_rule("git * reset * x * --hard", "deny")]},
            "git " + "reset x " * 5000 + "--hard",
            True,
        ),
        (
            {**_DENY_LIST, "rules": [_rule("git * reset * x * --hard", "deny")]},
            "git " + "reset x " * 5000,
            False,
        ),
    ],
)
def test_enforcer_decision_table(policy: dict[str, object], command: str, denied: bool) -> None:
    assert (_hook.decide(policy, command) is not None) is denied
