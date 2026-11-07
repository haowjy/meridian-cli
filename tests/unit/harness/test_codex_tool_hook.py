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
    "rules": [_rule(None, "deny"), *(_rule(p, "allow") for p in ("rg *", "cat *"))],
    "meridian_commands": [
        {"command": "meridian spawn done", "match": "prefix", "role": "completion"},
        {"command": "meridian work show", "match": "prefix", "role": "read"},
    ],
    "bash_fail_closed": True,
}

_WRAPPERS_ALLOWED = {
    **_ALLOWLIST,
    "rules": [_rule(None, "deny"), *(_rule(p, "allow") for p in ("rg *", "sudo *", "env *"))],
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
    # Wrapper options and keywords that used to hide the program.
    "time -p git reset --hard",
    "timeout 9.5 git reset --hard",
    "stdbuf -o L git reset --hard",
    "sudo -n git reset --hard",
    "sudo --user root git reset --hard",
    "env -S 'git reset --hard'",
    "env -u X -i git reset --hard",
    "nice -n 5 ionice -c 2 git reset --hard",
    "sudo -z git reset --hard",
    "coproc git reset --hard",
    "function f { git reset --hard; }; f",
    # Words the shell builds.
    "{git,reset,--hard}",
    "$'git' reset --hard",
    "git reset {--hard,x}",
    # Bodies the reader used to skip.
    "echo ${x:-$(git reset --hard)}",
    'echo "${x:-$(git reset --hard)}"',
    "echo $(( $(git reset --hard) + 1 ))",
    "echo `echo \\`git reset --hard\\``",
    "((1<<2))\ngit reset --hard\n2",
    "echo $(true # it's\n); git reset --hard",
    # `--` before the string, and forms the reader used to misread.
    "bash -c -- 'git reset --hard'",
    "bash -c -x 'git reset --hard'",
    "eval -- 'git reset --hard'",
    "((git reset --hard); echo)",
    "env --split-string='git reset --hard'",
    "env -vS'git reset --hard'",
    "/usr/bin/time -o out git reset --hard",
    # A shell that reads commands from stdin is fed by write_stdin.
    "bash",
    "sudo bash -s",
    "sh <<EOF\ngit reset --hard\nEOF",
    "echo hi | sh -",
    "bash /dev/stdin",
    # Nesting past what the reader follows.
    'bash -c "' * 9 + "git reset --hard",
]
# `git -C` and scripts are the named gaps; the rest are data, not commands.
_ALLOWED_BY_LIST = [
    "git -C . reset --hard",
    "find . -exec git reset --hard \\;",
    "bash script.sh",
    "bash -c 'git status'",
    "echo {a,b}",
    "echo $((1 + (2 * 3)))\n((i = 1 << 2))",
    # Asking a shell what it is runs nothing.
    "bash --version",
    "sh --help",
    "command -v bash",
    "command -V sh",
    "echo $'git reset --hard'",
    "time git status",
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
    # Bash syntax the grammar does not contain.
    "rg $'\\''; touch X #'",
    "rg ${x:-$(touch X)}",
    'rg "${x:-$(touch X)}"',
    "rg $(( $(touch X) + 1 ))",
    "rg `echo \\`touch X\\``",
    "coproc touch X",
    "function rg { touch X; }; rg x",
    "((1<<2))\ntouch X\n2",
    "PATH=/evil rg x",
    "PATH=/evil; rg x",
    "PATH=/evil meridian spawn done",
    "env PATH=/evil rg x",
    "rg x >& file",
    "rg x > out",
    "rg x < in",
    "rg {a,b}",
    "{ rg x; }",
    "(rg x)",
    "if true; then rg x; fi",
    "time rg x",
    "! rg x",
    "cat <<EOF\nrm x\nEOF",
    "rg x # comment",
    'rg "$HOME"',
    "rg x\\\ny",
    "rg \x00",
    # Wrapper options that moved the program.
    "sudo -n touch rg",
    "sudo -z rg x",
    "env -S 'rg x' ",
    "timeout rg touch X",
    # Programs by path that are not the system's.
    "./rg x",
    "/tmp/rg x",
    "sudo ./rg x",
    "bash -c '/tmp/rg x'",
    # Stdin shells.
    "bash",
    "bash -s",
    # An rc file runs before the -c string; any option beyond -e/-u/-x is refused.
    "bash --rcfile e.sh -ic 'rg x'",
    "bash --init-file e.sh -i -c 'rg x'",
    "bash -ic 'rg x'",
    "sh -i -c 'rg x'",
    "bash -lc 'rg x'",
    "bash --login -c 'rg x'",
    "bash -O extglob -c 'rg x'",
    "bash +O extglob -c 'rg x'",
    "bash --posix -c 'rg x'",
    "bash --anything-new -c 'rg x'",
    "env bash -ic 'rg x'",
    "/usr/bin/time -o out rg x",
    # A wrapper needs its own allow, not only the command it runs.
    "sudo rg x",
    "sudo -n rg x",
    "timeout 5 rg x",
    "env -u HOME rg x",
    "nice rg x",
    "xargs rg x",
    "sudo env rg x",
    "bash -c 'sudo rg x'",
]
_ALLOWED_BY_ALLOWLIST = [
    "rg foo | cat",
    "rg foo 2>/dev/null",
    "bash -c 'rg x'",
    "bash -ec 'rg x'",
    "sh -c -- 'rg x'",
    "/usr/bin/rg x",
    "rg 'a b' src",
    "rg 'a $b `c` \\d' src",
    "rg x 2>&1",
    "rg x &>/dev/null",
    "rg x &",
    "rg x && cat y || cat z",
    "rg x; cat y\ncat z",
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
        *((_WRAPPERS_ALLOWED, command, False) for command in ("sudo rg x", "sudo env rg x")),
        *((_WRAPPERS_ALLOWED, command, True) for command in ("timeout 5 rg x", "sudo cat x")),
        *(
            (_STAR_DENY, command, True)
            for command in ("touch x", "git status; touch x", "sudo git status")
        ),
        *((_STAR_DENY, command, False) for command in ("git status", "git log | git show")),
        *((_BASH_OVER_STAR, command, True) for command in ("rm -rf x", "sudo rm x")),
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
