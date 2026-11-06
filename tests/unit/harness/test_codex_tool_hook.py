"""The Codex PreToolUse enforcer's decision table: its shell parser and rule ranking."""

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


def _rule(pattern: str | None, action: str) -> dict[str, object]:
    return {
        "capability": "bash",
        "tool": "bash",
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
    "rules": [_rule(None, "deny"), *(_rule(p, "allow") for p in ("rg *", "cat *"))],
    "meridian_commands": [
        {"command": "meridian spawn done", "match": "prefix", "role": "completion"},
        {"command": "meridian work show", "match": "prefix", "role": "read"},
    ],
    "bash_fail_closed": True,
}

_DENIED_BY_LIST = [
    "git reset --hard",
    "git  reset --hard HEAD~1",
    "/usr/bin/git reset --hard",
    "FOO=1 git reset --hard",
    "sudo git reset --hard",
    "bash -c 'git reset --hard'",
    "sh -c \"bash -c 'git reset --hard'\"",
    "eval 'git reset --hard'",
    "echo $(git reset --hard)",
    "echo `git reset --hard`",
    "if true; then git reset --hard; fi",
    "{ git reset --hard; }",
    "(git reset --hard)",
    "echo a | git reset --hard",
    "git reset --hard 2>&1",
    "cat <<EOF\nx\nEOF\ngit reset --hard",
    "cat <<EOF\n$(git reset --hard)\nEOF",
]
# `git -C` and scripts are the named gaps; the rest are data, not commands.
_ALLOWED_BY_LIST = [
    "git -C . reset --hard",
    "sh do-reset.sh",
    "git status",
    "git reset --soft HEAD",
    "echo 'git reset --hard'",
    "cat <<'EOF'\ngit reset --hard\nEOF",
    "cat <<EOF\ngit reset --hard\nEOF",
    "echo hi # git reset --hard",
    "for f in a b; do echo $f; done",
]
_DENIED_BY_ALLOWLIST = [
    "rm -rf x",
    "rg foo > out.txt",
    "$CMD x",
    "bash -c 'rm x'",
    'bash -c "$X"',
    "rg $(rm x)",
    "meridian spawn done && rm x",
    "meridian work list",
    "git status; git reset --hard",
    "sh script.sh",
    "rg 'unterminated",
]
_ALLOWED_BY_ALLOWLIST = [
    "rg foo | cat",
    "rg foo 2>/dev/null",
    "bash -c 'rg x'",
    "sudo rg x",
    "/usr/bin/rg x",
    "rg 'a b' src",
    "cat <<EOF\nrm x\nEOF",
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
    ],
)
def test_enforcer_decision_table(policy: dict[str, object], command: str, denied: bool) -> None:
    assert (_hook.decide(policy, command) is not None) is denied
