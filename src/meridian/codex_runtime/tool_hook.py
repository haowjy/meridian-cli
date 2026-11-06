"""Codex ``PreToolUse`` hook: enforce a launch's scoped shell rules; stdlib only.

Codex runs this once per shell call. It must not import Meridian (a hook runs on every
shell call; see #721 for what a CLI import costs), so the rule ranking below mirrors the
Pi permission extension's ``policyDecision`` and the policy JSON is
``tool_extension_policy.ExtensionPolicy``.

The policy arrives in ``_MERIDIAN_TOOL_POLICY``. A missing variable denies. Exit 2 with the
reason on stderr is the block; any other failure exit is turned into a block by the hook
command's own wrapper (``codex_tool_hook.hook_command``).

Rules are decided per simple command, found by a shell tokenizer that sees through
wrappers (``env``, ``sudo``...), ``sh -c`` and ``eval`` strings, ``$(...)``, backticks,
subshells, and reserved words, and treats heredoc bodies as data. Deny at any layer wins; an
allow, and the allowlist's default deny, are decided at the innermost layer. What it cannot
see: ``sh script.sh``, ``python -c``, commands built at run time, and options placed between
a program and its subcommand (``git -C dir reset --hard``).
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import cast

POLICY_ENV = "_MERIDIAN_TOOL_POLICY"
DENIAL_PREFIX = "Blocked by Meridian policy: "
_MAX_DEPTH = 8

_WRAPPERS = frozenset(
    {
        "env",
        "sudo",
        "doas",
        "command",
        "builtin",
        "exec",
        "nohup",
        "timeout",
        "nice",
        "ionice",
        "stdbuf",
        "setsid",
        "xargs",
    }
)
_WRAPPER_OPTS_WITH_ARG = frozenset(
    {"-u", "-C", "-n", "-s", "-k", "-g", "-h", "-p", "--unset", "--chdir", "-I", "-P", "-L"}
    | {"-E", "-d", "-a", "-J", "-c"}
)
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash"})
# Words that introduce or close a compound command rather than name a program.
_RESERVED = frozenset(
    {"!", "{", "}", "if", "then", "else", "elif", "fi", "while", "until", "do", "done", "time"}
)
# Compound heads whose line holds no command to run.
_NOT_COMMANDS = frozenset({"for", "select", "case", "esac", "function", "[[", "]]", "in", "coproc"})
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
_GLOB_CHARS = frozenset("*?[")


@dataclass(frozen=True)
class Word:
    text: str
    # The shell expands it to a value this parser cannot know ($VAR, $(...), `...`, a glob).
    dynamic: bool = False
    quoted: bool = False


@dataclass
class Parsed:
    commands: list[list[Word]] = field(default_factory=lambda: cast("list[list[Word]]", []))
    # Why the input could not be fully read; None when it was.
    problem: str | None = None


def _matching_paren(src: str, start: int) -> int | None:
    """Index of the ``)`` closing the ``(`` before ``start``, honouring quotes."""

    depth = 1
    quote = ""
    i = start
    while i < len(src):
        ch = src[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 1
            elif ch == quote:
                quote = ""
        elif ch == "\\":
            i += 1
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _closing_backtick(src: str, start: int) -> int | None:
    i = start
    while i < len(src):
        if src[i] == "\\":
            i += 1
        elif src[i] == "`":
            return i
        i += 1
    return None


def _substitutions(src: str) -> list[str]:
    """Command texts inside ``$(...)`` and backticks of an unquoted heredoc body."""

    found: list[str] = []
    i = 0
    while i < len(src):
        if src.startswith("$(", i) and not src.startswith("$((", i):
            end = _matching_paren(src, i + 2)
            if end is None:
                break
            found.append(src[i + 2 : end])
            i = end
        elif src[i] == "`":
            end = _closing_backtick(src, i + 1)
            if end is None:
                break
            found.append(src[i + 1 : end])
            i = end
        elif src[i] == "\\":
            i += 1
        i += 1
    return found


class _Tokenizer:
    def __init__(self, src: str, depth: int) -> None:
        self.src = src
        self.depth = depth
        self.parsed = Parsed()
        self.words: list[Word] = []
        self.buffer: list[str] = []
        self.started = False
        self.dynamic = False
        self.quoted = False
        # Set after a redirection operator: the next word is its target.
        self.redirect: str | None = None
        self.heredocs: list[tuple[str, bool, bool]] = []

    def problem(self, reason: str) -> None:
        if self.parsed.problem is None:
            self.parsed.problem = reason

    def _substitute(self, text: str) -> None:
        if self.depth >= _MAX_DEPTH:
            self.problem("commands nested too deeply")
            return
        inner = parse(text, self.depth + 1)
        self.parsed.commands.extend(inner.commands)
        if inner.problem:
            self.problem(inner.problem)

    def end_word(self) -> None:
        if not self.started:
            return
        word = Word("".join(self.buffer), self.dynamic, self.quoted)
        self.buffer, self.started, self.dynamic, self.quoted = [], False, False, False
        target = self.redirect
        self.redirect = None
        if target is None:
            self.words.append(word)
        elif target in {"<<", "<<-"}:
            self.heredocs.append((word.text, not word.quoted, target == "<<-"))
        elif target == ">" and word.text != "/dev/null":
            self.problem("restricted bash does not accept redirection into a file")

    def end_command(self) -> None:
        self.end_word()
        if self.words:
            self.parsed.commands.append(self.words)
        self.words = []

    def add(self, text: str, *, dynamic: bool = False, quoted: bool = False) -> None:
        self.buffer.append(text)
        self.started = True
        self.dynamic = self.dynamic or dynamic
        self.quoted = self.quoted or quoted

    def heredoc_bodies(self, i: int) -> int:
        """Consume the bodies of pending heredocs starting at line start ``i``."""

        src = self.src
        for delimiter, expands, dash in self.heredocs:
            body: list[str] = []
            while True:
                if i >= len(src):
                    self.problem("here-document is not terminated")
                    break
                end = src.find("\n", i)
                line = src[i:] if end < 0 else src[i:end]
                i = len(src) if end < 0 else end + 1
                if (line.lstrip("\t") if dash else line) == delimiter:
                    break
                body.append(line)
            if expands:
                for text in _substitutions("\n".join(body)):
                    self._substitute(text)
        self.heredocs = []
        return i

    def run(self) -> Parsed:
        src = self.src
        i = 0
        while i < len(src):
            ch = src[i]
            if ch in " \t":
                self.end_word()
            elif ch == "\n":
                self.end_command()
                if self.heredocs:
                    i = self.heredoc_bodies(i + 1)
                    continue
            elif ch == "#" and not self.started:
                end = src.find("\n", i)
                i = len(src) if end < 0 else end
                continue
            elif ch == "\\":
                if i + 1 < len(src):
                    if src[i + 1] != "\n":
                        self.add(src[i + 1], quoted=True)
                    i += 1
            elif ch == "'":
                end = src.find("'", i + 1)
                if end < 0:
                    self.problem("a quote is not closed")
                    break
                self.add(src[i + 1 : end], quoted=True)
                i = end
            elif ch == '"':
                i = self._double_quoted(i)
                if i < 0:
                    break
            elif ch == "$":
                i = self._dollar(i)
                if i < 0:
                    break
            elif ch == "`":
                end = _closing_backtick(src, i + 1)
                if end is None:
                    self.problem("a backtick is not closed")
                    break
                self._substitute(src[i + 1 : end])
                self.add("", dynamic=True)
                i = end
            elif ch in "<>" and src.startswith("(", i + 1):
                end = _matching_paren(src, i + 2)
                if end is None:
                    self.problem("a process substitution is not closed")
                    break
                self._substitute(src[i + 2 : end])
                self.add("", dynamic=True)
                i = end
            elif ch in "<>":
                i = self._redirect(i)
            elif ch in ";|&()":
                i = self._separator(i)
            else:
                self.add(ch, dynamic=ch in _GLOB_CHARS)
            i += 1
        self.end_command()
        if self.heredocs:
            self.heredoc_bodies(len(src))
        return self.parsed

    def _double_quoted(self, i: int) -> int:
        src = self.src
        self.add("", quoted=True)
        i += 1
        while i < len(src):
            ch = src[i]
            if ch == '"':
                return i
            if ch == "\\" and i + 1 < len(src):
                if src[i + 1] in '$`"\\':
                    self.add(src[i + 1])
                elif src[i + 1] != "\n":
                    self.add(ch + src[i + 1])
                i += 2
                continue
            if ch == "`":
                end = _closing_backtick(src, i + 1)
                if end is None:
                    break
                self._substitute(src[i + 1 : end])
                self.add("", dynamic=True)
                i = end + 1
                continue
            if ch == "$":
                nxt = self._dollar(i)
                if nxt < 0:
                    return -1
                i = nxt + 1
                continue
            self.add(ch)
            i += 1
        self.problem("a quote is not closed")
        return -1

    def _dollar(self, i: int) -> int:
        """Handle ``$`` at ``i``; return the index of the last character consumed, or -1."""

        src = self.src
        if src.startswith("$((", i):
            end = _matching_paren(src, i + 2)
            if end is None:
                self.problem("an arithmetic expansion is not closed")
                return -1
            self.add("", dynamic=True)
            return end
        if src.startswith("$(", i):
            end = _matching_paren(src, i + 2)
            if end is None:
                self.problem("a command substitution is not closed")
                return -1
            self._substitute(src[i + 2 : end])
            self.add("", dynamic=True)
            return end
        if src.startswith("${", i):
            end = src.find("}", i + 2)
            if end < 0:
                self.problem("a parameter expansion is not closed")
                return -1
            self.add("", dynamic=True)
            return end
        match = re.match(r"\$([A-Za-z_][A-Za-z0-9_]*|[0-9@*#?$!-])", src[i:])
        if match is None:
            self.add("$")
            return i
        self.add("", dynamic=True)
        return i + len(match.group(0)) - 1

    def _redirect(self, i: int) -> int:
        src = self.src
        # A leading file-descriptor number belongs to the operator, not to a word.
        if self.started and "".join(self.buffer).isdigit() and not self.quoted:
            self.buffer, self.started, self.dynamic = [], False, False
        else:
            self.end_word()
        for op in ("<<<", "<<-", "<<", "<&", "<>", "<", ">>", ">|", ">&", ">"):
            if src.startswith(op, i):
                break
        if op in {"<<", "<<-"}:
            self.redirect = op
        elif op in {">", ">>", ">|"}:
            self.redirect = ">"
        else:
            self.redirect = "skip"
        return i + len(op) - 1

    def _separator(self, i: int) -> int:
        src = self.src
        ch = src[i]
        if ch == "&" and src.startswith("&>", i):
            # `&>` and `&>>` write stdout and stderr into a file.
            self.end_word()
            self.redirect = ">"
            return i + (2 if src.startswith("&>>", i) else 1)
        self.end_command()
        following = src[i + 1 : i + 2]
        if (following == ch and ch in ";|&") or (ch == "|" and following == "&"):
            return i + 1
        return i


def parse(src: str, depth: int = 0) -> Parsed:
    return _Tokenizer(src, depth).run()


@dataclass(frozen=True)
class Layered:
    """One simple command, outermost layer first; the last layer is what runs."""

    layers: tuple[tuple[str, ...], ...]
    # The program that runs is chosen at run time.
    dynamic: bool


def _base(argv: list[str]) -> str:
    return os.path.basename(argv[0]) if argv else ""


def _unwrap(argv: list[str]) -> list[str] | None:
    """``argv`` without its wrapper program and the wrapper's own options, or None."""

    if _base(argv) not in _WRAPPERS:
        return None
    rest = argv[1:]
    while rest and (
        _ASSIGNMENT.match(rest[0])
        or rest[0].startswith("-")
        or re.fullmatch(r"\d+[smhd]?", rest[0])
    ):
        option = rest.pop(0)
        if option in _WRAPPER_OPTS_WITH_ARG and rest:
            rest.pop(0)
    return rest


def _shell_string(argv: list[str]) -> str | None:
    """The command text a ``sh -c`` or ``eval`` runs."""

    program = _base(argv)
    if program == "eval":
        return " ".join(argv[1:])
    if program not in _SHELLS:
        return None
    i = 1
    while i < len(argv):
        word = argv[i]
        if word in {"-o", "-O", "+o", "+O"}:
            i += 2
        elif word.startswith("-") and not word.startswith("--") and "c" in word:
            return argv[i + 1] if i + 1 < len(argv) else None
        elif word.startswith(("-", "+")):
            i += 1
        else:
            return None
    return None


def layered_commands(parsed: Parsed, depth: int = 0) -> tuple[list[Layered], str | None]:
    """Every simple command of ``parsed`` with its wrapper layers, and a reading problem."""

    found: list[Layered] = []
    problem = parsed.problem
    for words in parsed.commands:
        while words and not words[0].quoted and words[0].text in _RESERVED:
            words = words[1:]
        if not words or (not words[0].quoted and words[0].text in _NOT_COMMANDS):
            continue
        while words and not words[0].quoted and _ASSIGNMENT.match(words[0].text):
            words = words[1:]
        if not words:
            continue
        argv = [word.text for word in words]
        layers = [tuple(argv)]
        # Peel wrappers; at a shell string, the commands inside run under these layers.
        while True:
            text = _shell_string(argv)
            if text is not None:
                if any(word.dynamic for word in words[1:]):
                    problem = problem or "a shell command string is built at run time"
                if depth >= _MAX_DEPTH:
                    problem = problem or "commands nested too deeply"
                    break
                inner, inner_problem = layered_commands(parse(text, depth + 1), depth + 1)
                problem = problem or inner_problem
                found.extend(Layered((*layers, *item.layers), item.dynamic) for item in inner)
                layers = []
                break
            rest = _unwrap(argv)
            if rest is None:
                break
            if not rest:
                layers = []
                break
            argv = rest
            layers.append(tuple(argv))
        if layers:
            found.append(Layered(tuple(layers), words[0].dynamic))
    return found, problem


def _glob_matches(pattern: str, value: str) -> bool:
    if pattern.endswith(" *") and value == pattern[:-2]:
        return True
    expression = ".*".join(re.escape(part) for part in pattern.split("*"))
    return re.fullmatch(expression, value, re.DOTALL) is not None


def _literal_length(pattern: str | None) -> int:
    return len(pattern.replace("*", "").rstrip()) if pattern else 0


_ACTION_RANK = {"deny": 2, "allow": 0}


@dataclass(frozen=True)
class Rule:
    pattern: str | None
    action: str
    label: str


def _winner(rules: list[Rule], command: str) -> Rule | None:
    """The rule deciding ``command``: scoped beats unscoped, then the longer literal, then deny."""

    best: tuple[tuple[int, int, int], Rule] | None = None
    for rule in rules:
        if rule.pattern is not None and not _glob_matches(rule.pattern, command):
            continue
        rank = (
            int(rule.pattern is not None),
            _literal_length(rule.pattern),
            _ACTION_RANK[rule.action],
        )
        if best is None or rank > best[0]:
            best = (rank, rule)
    return None if best is None else best[1]


def _forms(argv: tuple[str, ...]) -> list[str]:
    """The command as written and with its program's directory dropped (``/usr/bin/git``)."""

    written = " ".join(argv)
    bare = " ".join([os.path.basename(argv[0]), *argv[1:]])
    return [bare, written] if bare != written else [written]


def _verdict(rules: list[Rule], argv: tuple[str, ...]) -> Rule | None:
    """A scoped deny of either form wins, then a scoped allow, then the unscoped default."""

    winners = [winner for form in _forms(argv) if (winner := _winner(rules, form)) is not None]
    scoped = [winner for winner in winners if winner.pattern is not None]
    return next(
        (winner for winner in scoped if winner.action == "deny"),
        scoped[0] if scoped else winners[0] if winners else None,
    )


def _granted(grant: dict[str, object], text: str) -> bool:
    command = str(grant["command"])
    if grant["match"] == "exact":
        return text == command
    return text == command or text.startswith(f"{command} ")


def _rules(policy: dict[str, object]) -> list[Rule]:
    """The policy's shell rules; Codex has no way to ask, so ``ask`` rules are not carried."""

    rules: list[Rule] = []
    for raw in cast("list[dict[str, object]]", policy["rules"]):
        if raw["tool"] not in {"bash", "*"} or raw["action"] == "ask":
            continue
        pattern = raw.get("pattern")
        rules.append(
            Rule(
                pattern if isinstance(pattern, str) else None, str(raw["action"]), str(raw["label"])
            )
        )
    return rules


def _denial(rule: Rule, command: str) -> str:
    return f"{DENIAL_PREFIX}{rule.label} = {rule.action}. Attempted: {command}"


def decide(policy: dict[str, object], command: str) -> str | None:
    """The reason ``command`` is denied under ``policy``, or None to let it run."""

    fail_closed = bool(policy["bash_fail_closed"])
    commands, problem = layered_commands(parse(command))
    if problem is not None and fail_closed:
        return f"{DENIAL_PREFIX}the bash command could not be evaluated: {problem}."
    rules = _rules(policy)
    # Meridian's own commands are granted only as a whole command, as Pi grants them.
    if len(commands) == 1 and len(commands[0].layers) == 1:
        text = " ".join(commands[0].layers[0])
        grants = cast("list[dict[str, object]]", policy["meridian_commands"])
        if any(grant["role"] == "completion" and _granted(grant, text) for grant in grants):
            return None
        if any(_granted(grant, text) for grant in grants):
            rules += [
                Rule(
                    str(grant["command"]) if grant["match"] == "exact" else f"{grant['command']} *",
                    "allow",
                    "Meridian platform command",
                )
                for grant in grants
            ]
    scoped_denies = [rule for rule in rules if rule.pattern is not None and rule.action == "deny"]
    for item in commands:
        for outer in item.layers[:-1]:
            for rule in scoped_denies:
                assert rule.pattern is not None
                if any(_glob_matches(rule.pattern, form) for form in _forms(outer)):
                    return _denial(rule, command)
        if item.dynamic and fail_closed:
            return f"{DENIAL_PREFIX}the program of a bash command is chosen at run time."
        winner = _verdict(rules, item.layers[-1])
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
