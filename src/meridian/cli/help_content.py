"""Import-cheap shared help content for CLI command groups."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GroupHelp:
    """Help content shared by root help, group help, and agent curation."""

    summary: str
    long_help: str | None = None
    examples: tuple[tuple[str, str], ...] = ()
    agent_notes: str | None = None
    agent_subcommands: tuple[str, ...] | None = None


def render_group_help(group_name: str, *, agent_mode: bool) -> str:
    """Render the group description for human or agent help mode."""

    group = GROUPS[group_name]
    sections = [group.long_help or group.summary]

    if group.examples:
        example_lines: list[str] = []
        for invocation, note in group.examples:
            if note:
                example_lines.append(f"  {invocation}  # {note}")
            else:
                example_lines.append(f"  {invocation}")
        sections.append("Usage examples:\n\n" + "\n\n".join(example_lines))

    if agent_mode and group.agent_notes:
        sections.append("Agent Notes:\n\n" + group.agent_notes.rstrip())

    return "\n\n".join(sections)


GROUPS: dict[str, GroupHelp] = {
    "spawn": GroupHelp(
        summary="Run subagents with a model and prompt file.",
        long_help=(
            "Run subagents with a model and prompt file.\n"
            "Runs in foreground by default; returns when the spawn reaches a terminal state. "
            "Foreground streaming uses terminal capture when available (Unix TTY sessions). "
            "On Windows or non-TTY shells, meridian falls back to subprocess capture. "
            "Use --background to return immediately with the spawn ID. "
            "For delegation, write the prompt to a file and pass --prompt-file."
        ),
        examples=(
            ("meridian spawn -m gpt-5.3-codex --prompt-file /tmp/fix-auth.md --bg", ""),
            (
                "meridian spawn -m claude-sonnet-4-6 --prompt-file /tmp/review.md "
                "--bg -f src/main.py",
                "",
            ),
            ("meridian spawn --fork c123 --prompt-file /tmp/follow-up.md --bg", ""),
            (
                "meridian spawn --fork-fresh c123 -a reviewer --prompt-file "
                "/tmp/role-shift.md --bg",
                "",
            ),
            ("meridian spawn wait", ""),
        ),
        agent_notes=(
            "Core loop: launch detached with --bg, then drain with no-arg "
            "`meridian spawn wait`. Every --bg spawn must be drained before you "
            "respond to the user, start dependent work, or end your turn; "
            "un-waited background spawns are lost.\n\n"
            "Lifecycle: queued → running → finalizing → terminal. Treat "
            "finalizing as active; terminal is succeeded, failed, cancelled, or "
            "timed_out.\n\n"
            "Prompt passing: use --prompt-file for delegation. Inline -p is for "
            "trivial smoke tests only because shell quoting mutates prompts "
            "($vars, backticks, quotes, multiline) before meridian sees them.\n\n"
            "Profile vs model: prefer -a <profile> when a role fits; use "
            "-m <model> for one-offs. Put recurring model choices in profiles "
            "or config, not hardcoded spawn commands.\n\n"
            "Context refs: prefer a folder over a file bundle. A folder tells "
            "the agent where to work; it does not inline everything. Normal "
            "shape: one folder plus at most one source-of-truth file. "
            "AGENTS.md and .context/CONTEXT.md auto-load from the work "
            "directory, so don't pass them redundantly. Use --from for prior "
            "reasoning that is not in files.\n\n"
            "--goal is a verifiable exit state, not a restatement of the task.\n\n"
            "Parallel work: launch several --bg spawns, then one no-arg "
            "`meridian spawn wait` drains all. Don't babysit with repeated "
            "`spawn show` or `session log`; wait when you need results.\n\n"
            "Reuse decision: --continue resumes the same session; --fork "
            "branches while preserving identity; --fork-fresh branches and "
            "allows identity changes, with weaker cache locality; --from starts "
            "fresh while carrying prior context as reference.\n\n"
            "Inspect a run: `meridian session log <id>`; use "
            "`meridian spawn children <id>` to trace descendants.\n"
            "Steer a running spawn: `meridian spawn inject`.\n"
            "Stage spawn changes: `meridian spawn files <id> | xargs git add`."
        ),
        agent_subcommands=(
            "show",
            "wait",
            "list",
            "children",
            "files",
            "report",
            "inject",
            "done",
            "rearm",
            "cancel",
            "cancel-all",
        ),
    ),
    "session": GroupHelp(
        summary="Inspect transcripts and progress logs.",
        long_help=(
            "Inspect conversation and progress logs.\n\n"
            "Session refs accept three forms: chat ids (c123), spawn ids (p123),\n"
            "or raw harness session ids. Logs prefer harness transcripts when\n"
            "available and fall back to Meridian spawn output for active or\n"
            "transcriptless spawns. By default, commands operate on\n"
            "$MERIDIAN_CHAT_ID -- inherited from the spawning session -- so a\n"
            "subagent defaults to the top-level primary session log."
        ),
        agent_notes=(
            "REF forms: chat id (c123), spawn id (p123), or harness session id.\n\n"
            "Omitting REF defaults to the top-level primary session at every depth.\n"
            "Pass an explicit spawn id to inspect a specific spawn's transcript.\n\n"
            "Use `session search` to find transcript text; each hit prints a "
            "deterministic Open command you can run directly. Use `session log "
            "--segment previous` or `--global --around N` when navigating across "
            "compaction boundaries.\n\n"
            "Decision recovery: 'meridian work sessions WORK_ID --all'"
        ),
        agent_subcommands=("log", "search", "export"),
    ),
    "work": GroupHelp(
        summary="Work item dashboard and coordination.",
        long_help=(
            "Active activity grouped by work, plus work item coordination commands. "
            "Unassigned spawns appear under '(no work)'."
        ),
        agent_notes=(
            "Artifact placement: $MERIDIAN_ACTIVE_WORK_DIR for this item,\n"
            "$MERIDIAN_CONTEXT_KB_DIR for project-wide durable knowledge.\n\n"
            "Attach delegated runs with `meridian spawn ... --work WORK_ID` so "
            "spawns, sessions, and scratch artifacts stay grouped."
        ),
        agent_subcommands=(
            "start",
            "current",
            "root",
            "done",
            "sessions",
            "list",
            "show",
            "switch",
        ),
    ),
    "config": GroupHelp(
        summary="Show resolved configuration and sources.",
        long_help=(
            "Repository-level config (meridian.toml) for default\n"
            "agent, model, harness, timeouts, and output verbosity.\n\n"
            "Resolved values are evaluated independently per field -- a CLI\n"
            "override on one field does not pull other fields from the same\n"
            "source. Use `meridian config show` to see each value with its\n"
            "source annotation."
        ),
        agent_notes=(
            "Resolution is per field:\n"
            "CLI flag > env var > profile > project > user > harness default\n\n"
            "A CLI model override (-m) also drives harness routing."
        ),
        agent_subcommands=("show", "get", "set", "init"),
    ),
    "context": GroupHelp(
        summary="Show context paths for work and knowledge.",
        long_help="Show context paths for work and knowledge.",
    ),
    "doctor": GroupHelp(
        summary="Health check and orphan reconciliation.",
        long_help=(
            "Health check and auto-repair for meridian state.\n\n"
            "Reconciles orphaned spawns (dead PIDs, missing spawn directories),\n"
            "cleans stale session locks, scans telemetry retention, and warns about\n"
            "missing or malformed configuration.\n\n"
            "Use --kill-orphans to terminate orphaned spawn process groups before\n"
            "finalizing stale runs. Use --prune to delete stale spawn artifacts,\n"
            "telemetry segments, and other stale retention targets for the current\n"
            "project. Add --global to also prune stale orphan project dirs globally\n"
            "across ~/.meridian/projects/.\n\n"
            "Doctor is idempotent - re-running converges on the same result. It is\n"
            "safe (and intended) to run after a crash, after a force-kill, or any\n"
            "time `meridian spawn show` reports a status that doesn't match reality."
        ),
        examples=(
            ("meridian doctor", "check and repair"),
            ("meridian doctor --prune", "prune stale artifacts + telemetry"),
            ("meridian doctor --prune --global", "also prune other stale projects"),
            ("meridian doctor --kill-orphans", "terminate orphaned process groups"),
            ("meridian doctor --format text", "human-readable summary"),
        ),
        agent_notes=(
            "Run when a spawn seems stuck or status doesn't match reality.\n"
            "Spawn read paths (show, list, wait) and 'doctor' reconcile orphans.\n\n"
            "Common failure modes:\n\n"
            "  orphan_run              Runner died mid-flight. Relaunch.\n\n"
            "  orphan_finalization     Exited without finalizing. Check 'spawn show'\n"
            "                          for partial report.\n\n"
            "  Exit 127 / empty report Harness binary missing from PATH.\n\n"
            "  Exit 143 or 137         Check 'spawn show' first — if already\n"
            "                          succeeded, signal hit during cleanup.\n"
            "                          Otherwise retry.\n\n"
            "For the transcript: 'meridian session log SPAWN_ID'."
        ),
    ),
    "mars": GroupHelp(
        summary="Package management and agent materialization.",
        long_help="Forward all arguments to the bundled mars CLI.",
    ),
    "ext": GroupHelp(
        summary="Extension command discovery and invocation.",
        long_help="Extension command discovery and invocation.",
    ),
    "hooks": GroupHelp(
        summary="Hook inspection and execution commands.",
        long_help="Hook inspection and execution commands.",
    ),
    "sync": GroupHelp(
        summary="Sync operations and autosync conflict management.",
        long_help="Sync operations and autosync conflict management.",
    ),
    "models": GroupHelp(summary="Model catalog commands.", long_help="Model catalog commands."),
    "streaming": GroupHelp(
        summary="Streaming layer commands.", long_help="Streaming layer commands."
    ),
    "test": GroupHelp(
        summary="Focused test and demo commands.", long_help="Focused test and demo commands."
    ),
    "workspace": GroupHelp(
        summary="Workspace topology commands.",
        long_help=(
            "Workspace topology commands.\n\n"
            "Shared conventions live in meridian.toml [workspace.NAME] entries; "
            "local overrides and additions live in meridian.local.toml."
        ),
    ),
    "kg": GroupHelp(
        summary="Knowledge graph analysis: document relationships and link health.",
        long_help=(
            "Knowledge graph analysis: document relationships, tree topology,\n"
            "and broken link health."
        ),
        examples=(
            ("meridian kg graph", ""),
            ("meridian kg graph docs/", ""),
            ("meridian kg check", ""),
        ),
    ),
    "mermaid": GroupHelp(
        summary="Mermaid diagram validation.",
        long_help=(
            "Mermaid diagram validation: extract and check diagram syntax in\n"
            "markdown files and standalone .mmd/.mermaid files."
        ),
        examples=(
            ("meridian mermaid check", ""),
            ("meridian mermaid check docs/", ""),
            ("meridian mermaid check diagram.mmd", ""),
        ),
    ),
    "telemetry": GroupHelp(
        summary="Telemetry inspection: tail, query, and status over local segments.",
        long_help=(
            "Telemetry inspection: tail, query, and status over local segments.\n\n"
            "Note: Rootless MCP stdio server processes write telemetry to stderr only "
            "and are not visible in local segment readers."
        ),
    ),
    "completion": GroupHelp(
        summary="Shell completion helpers.", long_help="Shell completion helpers."
    ),
    "serve": GroupHelp(
        summary="Start FastMCP server on stdio.", long_help="Start FastMCP server on stdio."
    ),
    "init": GroupHelp(
        summary="Initialize meridian in a project.", long_help="Initialize meridian in a project."
    ),
    "chat": GroupHelp(
        summary="Start interactive chat service.", long_help="Start interactive chat service."
    ),
    "bootstrap": GroupHelp(
        summary="Bootstrap an agent runtime.", long_help="Bootstrap an agent runtime."
    ),
    "migrate": GroupHelp(
        summary="Run state and configuration migrations.",
        long_help="Run state and configuration migrations.",
    ),
    "qi": GroupHelp(
        summary="Inline knowledge navigation: AGENTS.md and .context/ locations.",
        long_help="Inline knowledge navigation: AGENTS.md and .context/ locations.",
    ),
}


__all__ = ["GROUPS", "GroupHelp", "render_group_help"]
