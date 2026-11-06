"""Codex ``PreToolUse`` hook: enforce a launch's scoped shell rules; stdlib only.

Codex runs this once per shell call. It must not import Meridian (a hook runs on every
shell call; see #721 for what a CLI import costs), so the rule ranking below mirrors the
Pi permission extension's ``policyDecision`` and the policy JSON is
``tool_extension_policy.ExtensionPolicy``.

The policy arrives in ``_MERIDIAN_TOOL_POLICY``. A missing variable denies. Exit 2 with the
reason on stderr is the block; any other failure exit is turned into a block by the hook
command's own wrapper (``codex_tool_hook.hook_command``).

Two readers share the rule ranking, chosen by ``bash_fail_closed``:

- **Fail closed** (an allowlist, or any rule that denies commands no scoped rule names): a
  command runs only if ``_strict_parse`` reads it whole. That grammar is small: simple
  commands joined by ``;``, ``&&``, ``||``, ``|``, ``&`` and newlines, words made of plain
  characters and quoted strings with no expansion, ``>/dev/null`` and ``2>&1``. Anything
  else (expansion, substitution, redirection, compound commands, assignments, programs by
  path outside the system bin directories) is refused, so a bash feature this file does not
  know about cannot slip through; it is refused too.
- **Best effort** (deny-lists): ``parse`` reads as much as it can, seeing through wrappers
  (``env``, ``sudo``...), ``sh -c`` and ``eval`` strings, ``$(...)``, backticks, subshells,
  braces and reserved words, and treats heredoc bodies as data. Deny at any layer wins.
  What it cannot see: ``sh script.sh``, ``python -c`` and other interpreters, wrappers it
  does not know, ``find -exec``, commands built at run time, options placed between a
  program and its subcommand (``git -C dir reset --hard``), and anything typed into an
  interpreter that is already running.

When any rule denies, a shell that reads its commands from stdin is refused: Codex's
``write_stdin`` would otherwise feed it commands this hook never sees.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from itertools import pairwise
from typing import cast

POLICY_ENV = "_MERIDIAN_TOOL_POLICY"
DENIAL_PREFIX = "Blocked by Meridian policy: "
_MAX_DEPTH = 8
_MAX_BRACE_WORDS = 64
_MAX_UNKNOWN_OPTIONS = 4

# Wrapper programs: (options that take a value, options that do not). Short options may be
# clustered (`-iu`) or carry their value (`-n5`); long ones carry it as `=value` or the next word.
_WrapperOptions = tuple[frozenset[str], frozenset[str]]


def _opts(takes: str, flags: str) -> _WrapperOptions:
    return frozenset(takes.split()), frozenset(flags.split())


_WRAPPERS: dict[str, _WrapperOptions] = {
    "env": _opts("-u -C --unset --chdir", "-i -0 -v --ignore-environment --null --debug"),
    "sudo": _opts(
        "-u -g -h -p -C -r -t -T -U --user --group --host --prompt --close-from --role --type "
        "--command-timeout --chdir --other-user",
        "-n -E -H -S -b -k -K -P -A -i -s --non-interactive --preserve-env --login --shell "
        "--background --reset-timestamp --stdin --set-home --askpass",
    ),
    "doas": _opts("-u -C", "-n -s"),
    "command": _opts("", "-p"),
    "builtin": _opts("", ""),
    "exec": _opts("-a", "-c -l"),
    "nohup": _opts("", ""),
    "timeout": _opts("-s -k --signal --kill-after", "-p -f -v --preserve-status --foreground"),
    "nice": _opts("-n --adjustment", "-0 -1 -2 -3 -4 -5 -6 -7 -8 -9"),
    "ionice": _opts("-c -n -p -P -u --class --classdata --pid --pgid --uid", "-t --ignore"),
    "stdbuf": _opts("-i -o -e --input --output --error", ""),
    "setsid": _opts("", "-c -f -w --ctty --fork --wait"),
    "xargs": _opts(
        "-I -n -P -L -s -d -a -E --replace --max-args --max-procs --max-lines --max-chars "
        "--delimiter --arg-file --eof",
        "-0 -r -t -p -x -o -i -l -e --null --no-run-if-empty --verbose --interactive --exit "
        "--open-tty",
    ),
    "time": _opts("-f -o --format --output", "-p -a -v --portability --append --verbose"),
}
# Wrappers that take a leading `NAME=value`.
_ASSIGNING_WRAPPERS = frozenset({"env", "sudo"})
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash"})
_STDIN_SCRIPTS = frozenset({"-", "/dev/stdin", "/dev/fd/0", "/proc/self/fd/0"})
# Words that introduce or close a compound command rather than name a program.
_RESERVED = frozenset(
    {
        "!",
        "{",
        "}",
        "if",
        "then",
        "else",
        "elif",
        "fi",
        "while",
        "until",
        "do",
        "done",
        "time",
        "coproc",
        "function",
    }
)
# Compound heads whose line holds no command to run.
_NOT_COMMANDS = frozenset({"for", "select", "case", "esac", "[[", "]]", "in"})
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
_GLOB_CHARS = frozenset("*?[")
_VARIABLE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*|[0-9@*#?$!-])")
# Where a program may live and still be matched by name: the system bin directories.
_SYSTEM_BIN_DIRS = frozenset({"/usr/bin", "/bin", "/usr/local/bin", "/usr/sbin", "/sbin"})

# Characters of a plain word in the fail-closed grammar. Everything else must be quoted.
_PLAIN_PUNCTUATION = frozenset("_-./,:@%+=^~*?[]")
_NULL_REDIRECT = re.compile(r"[012]?>>?/dev/null|&>>?/dev/null|[012]>&[012-]")

_ANSI_ESCAPE = re.compile(
    r"\\(?:x([0-9A-Fa-f]{1,2})|([0-7]{1,3})|u([0-9A-Fa-f]{1,4})|U([0-9A-Fa-f]{1,8})|(.))", re.DOTALL
)
_ANSI_SIMPLE = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "a": "\a",
    "b": "\b",
    "e": "\x1b",
    "E": "\x1b",
    "f": "\f",
    "v": "\v",
    "\\": "\\",
    "'": "'",
    '"': '"',
    "?": "?",
}


@dataclass(frozen=True)
class Word:
    text: str
    # The shell expands it to a value this parser cannot know ($VAR, $(...), `...`, a glob).
    dynamic: bool = False
    quoted: bool = False


@dataclass
class Parsed:
    commands: list[list[Word]] = field(default_factory=lambda: cast("list[list[Word]]", []))
    # Why the input was refused (outside the fail-closed grammar, or nested too deeply to
    # follow); None when it was read whole.
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


def _matching_brace(src: str, start: int) -> int | None:
    """Index of the ``}`` closing the ``${`` whose body starts at ``start``."""

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
        elif ch == "{":
            depth += 1
        elif ch == "}":
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


def _backtick_body(body: str) -> str:
    """The command a backtick body holds: the shell drops the backslash before ``$``, a
    backtick and a backslash, which is how backticks nest."""

    return re.sub(r"\\([$`\\])", r"\1", body)


def _substitutions(src: str) -> list[str]:
    """Command texts inside ``$(...)`` and backticks of a body that is not itself parsed."""

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
            found.append(_backtick_body(src[i + 1 : end]))
            i = end
        elif src[i] == "\\":
            i += 1
        i += 1
    return found


def _ansi_c(body: str) -> str | None:
    """The value of ``$'body'``, or None when it has an escape this reader does not decode."""

    undecodable = False

    def decode(match: re.Match[str]) -> str:
        nonlocal undecodable
        hex2, octal, hex4, hex8, simple = match.groups()
        try:
            if hex2 is not None:
                return chr(int(hex2, 16))
            if octal is not None:
                return chr(int(octal, 8) & 0xFF)
            if hex4 is not None or hex8 is not None:
                return chr(int(hex4 or hex8, 16))
        except (ValueError, OverflowError):
            pass
        if simple in _ANSI_SIMPLE:
            return _ANSI_SIMPLE[simple]
        undecodable = True
        return ""

    text = _ANSI_ESCAPE.sub(decode, body)
    return None if undecodable else text


def _brace_expand(text: str) -> list[str]:
    """Bash's ``{a,b}`` expansion of an unquoted word, capped; ``{1..3}`` ranges are left."""

    start = text.find("{")
    while start >= 0:
        depth = 0
        commas: list[int] = []
        end = -1
        for j in range(start, len(text)):
            ch = text[j]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = j
                    break
            elif ch == "," and depth == 1:
                commas.append(j)
        if end < 0:
            return [text]
        if commas:
            cuts = [start, *commas, end]
            out: list[str] = []
            for a, b in pairwise(cuts):
                out.extend(_brace_expand(text[:start] + text[a + 1 : b] + text[end + 1 :]))
                if len(out) > _MAX_BRACE_WORDS:
                    break
            return out
        start = text.find("{", start + 1)
    return [text]


class _Tokenizer:
    """The best-effort reader: splits a command line into simple commands."""

    def __init__(self, src: str, depth: int) -> None:
        self.src = src
        self.depth = depth
        self.parsed = Parsed()
        self.words: list[Word] = []
        self.buffer: list[str] = []
        self.started = False
        self.dynamic = False
        self.quoted = False
        self.brace = False
        # Set after a redirection operator: the next word is its target (or a heredoc marker).
        self.redirect: str | None = None
        self.heredocs: list[tuple[str, bool, bool]] = []

    def _substitute(self, text: str) -> None:
        if self.depth >= _MAX_DEPTH:
            self.parsed.problem = self.parsed.problem or "commands nested too deeply"
            return
        inner = parse(text, self.depth + 1)
        self.parsed.commands.extend(inner.commands)
        self.parsed.problem = self.parsed.problem or inner.problem

    def _substitute_all(self, body: str) -> None:
        for text in _substitutions(body):
            self._substitute(text)

    def end_word(self) -> None:
        if not self.started:
            return
        text = "".join(self.buffer)
        word = Word(text, self.dynamic, self.quoted)
        braced = self.brace
        self.buffer, self.started, self.dynamic, self.quoted, self.brace = (
            [],
            False,
            False,
            False,
            False,
        )
        target = self.redirect
        self.redirect = None
        if target is None:
            if braced:
                self.words.extend(
                    Word(item, word.dynamic, word.quoted) for item in _brace_expand(text)
                )
            else:
                self.words.append(word)
        elif target in {"<<", "<<-"}:
            self.heredocs.append((word.text, not word.quoted, target == "<<-"))

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
            while i < len(src):
                end = src.find("\n", i)
                line = src[i:] if end < 0 else src[i:end]
                i = len(src) if end < 0 else end + 1
                if (line.lstrip("\t") if dash else line) == delimiter:
                    break
                body.append(line)
            if expands:
                self._substitute_all("\n".join(body))
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
                    break
                self.add(src[i + 1 : end], quoted=True)
                i = end
            elif ch == '"':
                i = self._double_quoted(i)
                if i < 0:
                    break
            elif ch == "$":
                i = self._dollar(i)
            elif ch == "`":
                end = _closing_backtick(src, i + 1)
                if end is None:
                    break
                self._substitute(_backtick_body(src[i + 1 : end]))
                self.add("", dynamic=True)
                i = end
            elif ch in "<>" and src.startswith("(", i + 1):
                end = _matching_paren(src, i + 2)
                if end is None:
                    break
                self._substitute(src[i + 2 : end])
                self.add("", dynamic=True)
                i = end
            elif ch in "<>":
                i = self._redirect(i)
            elif (
                ch == "("
                and src.startswith("((", i)
                and not self.started
                and self._arithmetic_head()
            ):
                # `((...))` is arithmetic: its `<<` is a shift, not a here-document.
                end = _matching_paren(src, i + 1)
                if end is None:
                    break
                self._substitute_all(src[i + 2 : end - 1])
                self.end_command()
                i = end
            elif ch in ";|&()":
                i = self._separator(i)
            else:
                if ch == "{":
                    self.brace = True
                self.add(ch, dynamic=ch in _GLOB_CHARS)
            i += 1
        self.end_command()
        if self.heredocs:
            self.heredoc_bodies(len(src))
        return self.parsed

    def _arithmetic_head(self) -> bool:
        return not self.words or [word.text for word in self.words] == ["for"]

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
                self._substitute(_backtick_body(src[i + 1 : end]))
                self.add("", dynamic=True)
                i = end + 1
                continue
            if ch == "$":
                i = self._dollar(i, in_quotes=True) + 1
                continue
            self.add(ch)
            i += 1
        return -1

    def _dollar(self, i: int, *, in_quotes: bool = False) -> int:
        """Handle ``$`` at ``i``; return the index of the last character consumed."""

        src = self.src
        if src.startswith("$((", i):
            end = _matching_paren(src, i + 2)
            if end is None:
                return len(src)
            self._substitute_all(src[i + 3 : end - 1])
            self.add("", dynamic=True)
            return end
        if src.startswith("$(", i):
            end = _matching_paren(src, i + 2)
            if end is None:
                return len(src)
            self._substitute(src[i + 2 : end])
            self.add("", dynamic=True)
            return end
        if src.startswith("${", i):
            end = _matching_brace(src, i + 2)
            if end is None:
                return len(src)
            self._substitute_all(src[i + 2 : end])
            self.add("", dynamic=True)
            return end
        if not in_quotes and src.startswith("$'", i):
            end = i + 2
            while end < len(src) and src[end] != "'":
                end += 2 if src[end] == "\\" else 1
            if end >= len(src):
                return len(src)
            value = _ansi_c(src[i + 2 : end])
            self.add(value or "", dynamic=value is None, quoted=True)
            return end
        if not in_quotes and src.startswith('$"', i):
            return i  # a locale-translated string: the quote that follows opens it as usual
        match = _VARIABLE.match(src, i)
        if match is None:
            self.add("$")
            return i
        self.add("", dynamic=True)
        return match.end() - 1

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
        self.redirect = op if op in {"<<", "<<-"} else "skip"
        return i + len(op) - 1

    def _separator(self, i: int) -> int:
        src = self.src
        ch = src[i]
        if ch == "&" and src.startswith("&>", i):
            # `&>` and `&>>` write stdout and stderr into a file.
            self.end_word()
            self.redirect = "skip"
            return i + (2 if src.startswith("&>>", i) else 1)
        self.end_command()
        following = src[i + 1 : i + 2]
        if (following == ch and ch in ";|&") or (ch == "|" and following == "&"):
            return i + 1
        return i


def parse(src: str, depth: int = 0) -> Parsed:
    return _Tokenizer(src, depth).run()


def _strict_parse(src: str) -> Parsed:
    """The fail-closed reader: the whole command line, or the first thing outside the grammar.

    Words are plain characters and quoted strings. Inside single quotes anything is data;
    inside double quotes ``$``, a backtick and a backslash would expand, so they are refused.
    ``<`` and ``>`` are accepted only as the word forms in ``_NULL_REDIRECT``.
    """

    parsed = Parsed()
    words: list[Word] = []
    buffer: list[str] = []
    started = quoted = dynamic = redirecting = False

    def refuse(reason: str) -> Parsed:
        parsed.problem = reason
        return parsed

    def end_word() -> str | None:
        nonlocal buffer, started, quoted, dynamic, redirecting
        if not started:
            return None
        text = "".join(buffer)
        is_redirect = redirecting
        was_quoted, was_dynamic = quoted, dynamic
        buffer, started, quoted, dynamic, redirecting = [], False, False, False, False
        if is_redirect:
            if was_quoted or not _NULL_REDIRECT.fullmatch(text):
                return f"redirection {text!r} (only >/dev/null and 2>&1 are accepted)"
            return None
        words.append(Word(text, was_dynamic, was_quoted))
        return None

    def end_command() -> str | None:
        nonlocal words
        problem = end_word()
        if problem is None and words:
            head = words[0].text
            if (not words[0].quoted and head in _RESERVED | _NOT_COMMANDS) or _ASSIGNMENT.match(
                head
            ):
                problem = f"{head!r} starts a compound command or an assignment"
            else:
                parsed.commands.append(words)
        words = []
        return problem

    i = 0
    while i < len(src):
        ch = src[i]
        problem: str | None = None
        if ch in " \t":
            problem = end_word()
        elif ch in "\n;|":
            problem = end_command()
        elif ch == "&" and (src.startswith(">", i + 1) or (buffer and buffer[-1] == ">")):
            buffer.append(ch)
            started = redirecting = True
        elif ch == "&":
            problem = end_command()
        elif ch in "<>":
            buffer.append(ch)
            started = redirecting = True
        elif ch == "'":
            end = src.find("'", i + 1)
            if end < 0:
                return refuse("a quote is not closed")
            buffer.append(src[i + 1 : end])
            started = quoted = True
            i = end
        elif ch == '"':
            end = src.find('"', i + 1)
            if end < 0:
                return refuse("a quote is not closed")
            body = src[i + 1 : end]
            if any(c in body for c in "$`\\"):
                return refuse("a double-quoted string expands (use single quotes)")
            buffer.append(body)
            started = quoted = True
            i = end
        elif ch.isalnum() or ch in _PLAIN_PUNCTUATION:
            buffer.append(ch)
            started = True
            dynamic = dynamic or ch in _GLOB_CHARS
        else:
            return refuse(f"{ch!r} is outside the shell syntax a restricted bash accepts")
        if problem is not None:
            return refuse(problem)
        i += 1
    problem = end_command()
    if problem is not None:
        return refuse(problem)
    return parsed


@dataclass(frozen=True)
class Layered:
    """One simple command, outermost layer first; the last layer is what runs."""

    layers: tuple[tuple[str, ...], ...]
    # The program that runs is chosen at run time.
    dynamic: bool
    # A `NAME=value` precedes the program, so the environment it runs in is not the agent's.
    assigned: bool


def _base(argv: list[str]) -> str:
    return os.path.basename(argv[0]) if argv else ""


def _option_width(options: _WrapperOptions, word: str) -> int | None:
    """How many words a wrapper option occupies (1 or 2), or None when it is not in the table."""

    takes, flags = options
    if word.startswith("--"):
        name, equals, _ = word.partition("=")
        if name in takes:
            return 1 if equals else 2
        return 1 if name in flags else None
    if word[:2] in takes:
        return 1 if len(word) > 2 else 2
    return 1 if all(f"-{c}" in flags for c in word[1:]) else None


def _peel(argv: list[str], *, strict: bool) -> tuple[list[list[str]], bool, str | None] | None:
    """What a wrapper command runs: candidate argvs, whether it set variables, a problem.

    None when ``argv`` is not a wrapper. An option the table does not list is a problem under
    the fail-closed reader; the best-effort reader tries both readings (a flag, and an option
    that takes the next word) and checks every candidate.
    """

    name = _base(argv)
    options = _WRAPPERS.get(name)
    if options is None:
        return None
    assigned = False
    problem: str | None = None

    def scan(rest: list[str], unknown: int) -> list[list[str]]:
        nonlocal assigned, problem
        while rest:
            word = rest[0]
            if name in _ASSIGNING_WRAPPERS and _ASSIGNMENT.match(word):
                assigned = True
                if strict:
                    problem = problem or f"{name} sets a variable for the program it runs"
                    return []
                rest = rest[1:]
            elif word == "--":
                rest = rest[1:]
                break
            elif not word.startswith("-") or word == "-":
                break
            elif name == "env" and (word == "--split-string" or word.startswith("-S")):
                if strict:
                    problem = problem or "env -S splits a string at run time"
                    return []
                value = word.partition("=")[2] if word.startswith("--") else word[2:]
                if not value and len(rest) > 1:
                    value, rest = rest[1], rest[1:]
                rest = [*value.split(), *rest[1:]]
            else:
                width = _option_width(options, word)
                if width is None:
                    if strict:
                        problem = problem or f"{name} option {word!r} is not understood"
                        return []
                    if unknown >= _MAX_UNKNOWN_OPTIONS:
                        width = 1
                    else:
                        return scan(rest[1:], unknown + 1) + scan(rest[2:], unknown + 1)
                rest = rest[width:]
        if name == "timeout" and rest:
            rest = rest[1:]  # the duration
        return [rest]

    candidates = scan(argv[1:], 0)
    return candidates, assigned, problem


def _shell_run(argv: list[str]) -> tuple[str, str | None] | None:
    """What a shell or ``eval`` command runs: ``("string", text)``, ``("script", None)``
    or ``("stdin", None)``; None when ``argv`` is neither."""

    program = _base(argv)
    if program == "eval":
        return "string", " ".join(argv[1:])
    if program not in _SHELLS:
        return None
    i = 1
    stdin = False
    while i < len(argv):
        word = argv[i]
        if word in {"-o", "-O", "+o", "+O", "--rcfile", "--init-file"}:
            i += 2
        elif word == "--":
            i += 1
            break
        elif word.startswith("-") and not word.startswith("--") and "c" in word[1:]:
            return ("string", argv[i + 1]) if i + 1 < len(argv) else ("script", None)
        elif word.startswith(("-", "+")) and word != "-":
            stdin = stdin or (word.startswith("-") and not word.startswith("--") and "s" in word)
            i += 1
        else:
            break
    if stdin or i >= len(argv) or argv[i] in _STDIN_SCRIPTS:
        return "stdin", None
    return "script", None


def _reads_stdin(argv: tuple[str, ...]) -> bool:
    run = _shell_run(list(argv))
    return run is not None and run[0] == "stdin"


def _program_problem(argv: list[str]) -> str | None:
    """Fail-closed: a program given by path matches a rule by name only in a system bin dir."""

    program = argv[0]
    if "/" in program and os.path.dirname(program) not in _SYSTEM_BIN_DIRS:
        return f"the program {program!r} is run by a path outside the system bin directories"
    return None


def layered_commands(
    parsed: Parsed, *, strict: bool, depth: int = 0
) -> tuple[list[Layered], str | None]:
    """Every simple command of ``parsed`` with its wrapper layers, and a reading problem."""

    found: list[Layered] = []
    problem = parsed.problem

    def resolve(
        argv: list[str], layers: list[tuple[str, ...]], dynamic: bool, assigned: bool
    ) -> None:
        nonlocal problem
        if strict:
            problem = problem or _program_problem(argv)
        run = _shell_run(argv)
        if run is not None and run[0] == "string":
            if depth >= _MAX_DEPTH:
                problem = problem or "commands nested too deeply"
                return
            inner_parsed = _strict_parse(run[1] or "") if strict else parse(run[1] or "", depth + 1)
            inner, inner_problem = layered_commands(inner_parsed, strict=strict, depth=depth + 1)
            problem = problem or inner_problem
            found.extend(
                Layered((*layers, *item.layers), item.dynamic, assigned or item.assigned)
                for item in inner
            )
            return
        peeled = _peel(argv, strict=strict)
        if peeled is None:
            found.append(Layered(tuple(layers), dynamic, assigned))
            return
        candidates, set_variables, peel_problem = peeled
        problem = problem or peel_problem
        for rest in candidates:
            if rest:
                resolve(rest, [*layers, tuple(rest)], dynamic, assigned or set_variables)
            else:
                found.append(Layered(tuple(layers), dynamic, assigned or set_variables))

    for words in parsed.commands:
        leading = 0
        while (
            leading < len(words) and not words[leading].quoted and words[leading].text in _RESERVED
        ):
            was_time = words[leading].text == "time"
            was_function = words[leading].text == "function"
            leading += 1
            if was_time:
                while leading < len(words) and words[leading].text in {"-p", "--"}:
                    leading += 1
            elif was_function:
                leading += 1  # the function's name
        words = words[leading:]
        if not words or (not words[0].quoted and words[0].text in _NOT_COMMANDS):
            continue
        assigned = False
        while words and not words[0].quoted and _ASSIGNMENT.match(words[0].text):
            words = words[1:]
            assigned = True
        if not words:
            continue
        argv = [word.text for word in words]
        resolve(argv, [tuple(argv)], words[0].dynamic, assigned)
    return found, problem


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


def _literal_length(pattern: str | None) -> int:
    return len(pattern.replace("*", "").rstrip()) if pattern else 0


_ACTION_RANK = {"deny": 2, "allow": 0}


@dataclass(frozen=True)
class Rule:
    tool: str
    pattern: str | None
    action: str
    label: str


def _winner(rules: list[Rule], command: str) -> Rule | None:
    """The rule deciding ``command``: scoped beats bash's own unscoped rule, which beats
    ``*``; then the longer literal; then deny."""

    best: tuple[tuple[int, int, int], Rule] | None = None
    for rule in rules:
        if rule.pattern is not None and not _glob_matches(rule.pattern, command):
            continue
        tier = 2 if rule.pattern is not None else int(rule.tool != "*")
        rank = (tier, _literal_length(rule.pattern), _ACTION_RANK[rule.action])
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

    fail_closed = bool(policy["bash_fail_closed"])
    parsed = _strict_parse(command) if fail_closed else parse(command)
    commands, problem = layered_commands(parsed, strict=fail_closed)
    if problem is not None:
        return f"{DENIAL_PREFIX}the bash command could not be evaluated: {problem}."
    rules = _rules(policy)
    # Meridian's own commands are granted only as a whole command, as Pi grants them, and
    # never under a changed environment (`PATH=/evil meridian spawn done`).
    if len(commands) == 1 and len(commands[0].layers) == 1 and not commands[0].assigned:
        text = " ".join(commands[0].layers[0])
        grants = cast("list[dict[str, object]]", policy["meridian_commands"])
        if any(grant["role"] == "completion" and _granted(grant, text) for grant in grants):
            return None
        if any(_granted(grant, text) for grant in grants):
            rules += [
                Rule(
                    "bash",
                    str(grant["command"]) if grant["match"] == "exact" else f"{grant['command']} *",
                    "allow",
                    "Meridian platform command",
                )
                for grant in grants
            ]
    scoped_denies = [rule for rule in rules if rule.pattern is not None and rule.action == "deny"]
    denies = any(rule.action == "deny" for rule in rules)
    for item in commands:
        for outer in item.layers[:-1]:
            for rule in scoped_denies:
                assert rule.pattern is not None
                if any(_glob_matches(rule.pattern, form) for form in _forms(outer)):
                    return _denial(rule, command)
        if item.dynamic and fail_closed:
            return f"{DENIAL_PREFIX}the program of a bash command is chosen at run time."
        if denies and _reads_stdin(item.layers[-1]):
            return (
                f"{DENIAL_PREFIX}a shell that reads its commands from stdin cannot be checked. "
                f"Attempted: {command}"
            )
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
