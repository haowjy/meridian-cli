"""Authoritative registry for CLI command groups.

Owns help content, root-help curation, group app linkage, and lazy-registration
bucket mapping. Root help, curated help, and command registration derive from
``COMMAND_GROUP_SPECS`` — do not duplicate parallel tables elsewhere.
"""

# ruff: noqa: E501

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cyclopts import App


@dataclass(frozen=True)
class GroupHelp:
    """Help content shared by root help, group help, and agent curation."""

    summary: str
    long_help: str | None = None
    examples: tuple[tuple[str, str], ...] = ()
    agent_notes: str | None = None
    agent_notes_brief: str | None = None
    agent_subcommands: tuple[str, ...] | None = None
    agent_core_subcommands: tuple[str, ...] | None = None


@dataclass(frozen=True)
class CommandGroupSpec:
    """Metadata for one top-level CLI command group."""

    help: GroupHelp
    agent_root_description: str | None = None
    agent_root: bool = False
    human_root_order: int | None = None
    registration_bucket: str | None = None
    has_group_app: bool = False
    hidden_from_root_help: bool = False


_group_apps: dict[str, App] = {}


_GROUP_HELP: dict[str, GroupHelp] = {
    "spawn": GroupHelp(
        summary='Run subagents with a model and prompt file.',
        long_help='Hand a task to a subagent — a bulk read, a parallel fix, or a job for a stronger or cheaper model — so you keep your own context for the parts that need your judgment. Write the task to a file and pass --prompt-file; add --bg to launch detached and collect the result with `meridian spawn wait`.',
        examples=(('meridian work path prompts/fix-auth.md', 'resolve, write, then paste the printed path below'), ('meridian spawn -m gpt-5.3-codex --prompt-file /absolute/work/path/prompts/fix-auth.md --bg', ''), ('meridian work path prompts/review.md', 'resolve, write, then paste the printed path below'), ('meridian spawn -m claude-sonnet-4-6 --prompt-file /absolute/work/path/prompts/review.md --bg -f src/main.py', ''), ('meridian work path prompts/follow-up.md', 'resolve, write, then paste the printed path below'), ('meridian spawn --fork c123 --prompt-file /absolute/work/path/prompts/follow-up.md --bg', ''), ('meridian work path prompts/role-shift.md', 'resolve, write, then paste the printed path below'), ('meridian spawn --fork-fresh c123 -a reviewer --prompt-file /absolute/work/path/prompts/role-shift.md --bg', ''), ('meridian spawn wait', '')),
        agent_notes="Core loop: launch detached with --bg, then drain with no-arg `meridian spawn wait`. Every --bg spawn must be drained before you respond to the user, start dependent work, or end your turn; un-waited background spawns are lost.\n\nCompletion handshake: before a child closes, Meridian may ask it to confirm. Inside that child, run `meridian spawn done` when its work and descendants are finished, or `meridian spawn rearm` when more work remains. The inherited spawn id targets the current child, so omit it.\n\nLifecycle: running → terminal. A spawn is running until it ends; terminal is succeeded, failed, cancelled, or timed_out.\n\nPrompt passing: write the brief under the active work directory's `prompts/` and pass its absolute path to --prompt-file. Inline -p is for trivial smoke tests only because shell quoting mutates prompts ($vars, backticks, quotes, multiline) before meridian sees them. Run `meridian work path prompts/<name>.md`, write to the printed path, then pass that literal in a separate spawn call (no /tmp, command substitution, or shell variable).\n\nProfile vs model: prefer -a <profile> when a role fits; use -m <model> for one-offs. Put recurring model choices in profiles or config, not hardcoded spawn commands.\n\nContext refs: prefer a folder over a file bundle. A folder tells the agent where to work; it does not inline everything. Normal shape: one folder plus at most one source-of-truth file. Use --from for prior reasoning that is not in files.\n\nTask dir: source edits run in the work item's task_dir (check: `meridian task-dir`). --task-dir points one spawn at another checkout, e.g. a worktree lane; `meridian work task-dir <path>` rebinds the work item itself.\n\n--goal is a verifiable exit state, not a restatement of the task.\n\nParallel work: launch several --bg spawns, then one no-arg `meridian spawn wait` drains all. Don't babysit with repeated `spawn show` or `session log`; wait when you need results.\n\nReuse decision: --continue resumes the same session; --fork branches while preserving identity; --fork-fresh branches and allows identity changes, with weaker cache locality; --from starts fresh while carrying prior context as reference.\n\nInspect a run: `meridian session log <id>`; `meridian spawn children` traces this spawn's descendants.\nMessage a running spawn or another live session: `meridian spawn message`; read `meridian skills messaging__meridian`.",
        agent_notes_brief='--goal is a verifiable exit state, not a task restatement.\n\nContext: --continue resumes and keeps the spawn ID; --fork branches with the same identity; --fork-fresh allows identity changes; --from starts fresh with prior context.\n\nDo not poll with `spawn show` or `session log`; `spawn wait` returns when results are ready.\n\nMore: `meridian spawn -h --advanced`.',
        agent_subcommands=('show', 'wait', 'list', 'children', 'report', 'message', 'done', 'rearm', 'cancel', 'cancel-all'),
        agent_core_subcommands=('wait', 'show', 'list', 'message', 'done', 'rearm', 'cancel'),
    ),
    "session": GroupHelp(
        summary='Browse sessions or inspect transcripts and progress logs.',
        long_help="Read back what a session did — its conversation and progress logs — to recover a subagent's reasoning or find what was decided.\n\nRefs take five forms: chat ids (c123), spawn ids (p123), raw harness session ids, a history UUID, or @latest / @last-failed / @last-completed. `log` and `export` need a ref (or `--file`); omitting it is an error. `search` without a ref searches the whole project's sessions instead of one. A chat id names that chat's own session; pass a spawn id to read one spawn's transcript.",
        agent_notes="Use `session search` to find transcript text; each hit prints a deterministic Open command you can run directly. Cross compaction boundaries with `session log --segment previous` or `--global --around N`.\n\nRecover decisions across a work item's sessions with `meridian work sessions WORK_ID --all`.",
        agent_subcommands=('browse', 'log', 'search', 'export', 'archive', 'import', 'restore', 'index'),
        agent_core_subcommands=('browse', 'log', 'search', 'export'),
    ),
    "work": GroupHelp(
        summary='Work item dashboard and coordination.',
        long_help="Group related runs under a work item — a shared id that keeps spawns, sessions, and scratch artifacts together so you can find them later. This view lists active runs grouped by work item; unassigned spawns appear under '(no work)'.",
        agent_subcommands=('start', 'current', 'root', 'path', 'task-dir', 'update', 'done', 'sessions', 'list', 'show', 'switch', 'clear', 'rename', 'reopen', 'delete'),
        agent_core_subcommands=('start', 'current', 'root', 'path', 'task-dir', 'update', 'done', 'sessions', 'list', 'show', 'switch', 'clear'),
    ),
    "config": GroupHelp(
        summary='Show resolved configuration and sources.',
        long_help='Repository-level config (meridian.toml) for default agent, model, harness, timeouts, and output verbosity.\n\nResolved values are evaluated independently per field -- a CLI override on one field does not pull other fields from the same source. Use `meridian config show` to see each value with its source annotation.',
        agent_notes='Resolution is per field:\nCLI flag > env var > profile > project > user > harness default\n\nA CLI model override (-m) also drives harness routing.',
        agent_subcommands=('show', 'get', 'set', 'init'),
    ),
    "context": GroupHelp(
        summary='Show context paths for work and knowledge.',
        long_help='Resolve context paths for handoffs and artifact placement — the active work dir and knowledge-base roots. With no args it prints the set; pass --name for one path.',
    ),
    "doctor": GroupHelp(
        summary='Health check and orphan reconciliation.',
        long_help="Health check and auto-repair for meridian state.\n\nReconciles crashed spawns and missing spawn directories, cleans stale session locks, scans telemetry retention, and warns about missing or malformed configuration.\n\nUse --kill-orphans to terminate orphaned spawn process groups before finalizing stale runs. Use --prune to delete stale spawn artifacts, telemetry segments, and other stale retention targets for the current project. Add --global to also prune stale orphan project dirs globally across ~/.meridian/projects/.\n\nDoctor is idempotent - re-running converges on the same result. Spawn show/list/wait already project runner crashes from the launch lock; run doctor after a crash or force-kill to persist outcomes and clean scopes.",
        examples=(('meridian doctor', 'check and repair'), ('meridian doctor --prune', 'prune stale artifacts + telemetry'), ('meridian doctor --prune --global', 'also prune other stale projects'), ('meridian doctor --kill-orphans', 'terminate orphaned process groups'), ('meridian doctor --format text', 'human-readable summary')),
        agent_notes="Run when a spawn seems stuck or status doesn't match reality.\nSpawn read paths (show, list, wait) project runner crashes; 'doctor' persists them and cleans up recorded processes.\n\nCommon failure modes:\n\n  crashed                 Runner died without an outcome. Check report.md;\n                          relaunch or use --continue. A cancel request\n                          produces cancelled instead.\n\n  Exit 127 / empty report Harness binary missing from PATH.\n\n  Exit 143 or 137         Check 'spawn show' first — if already\n                          succeeded, signal hit during cleanup.\n                          Otherwise retry.\n\nFor the transcript: 'meridian session log SPAWN_ID'.",
    ),
    "mars": GroupHelp(
        summary='Package management and agent materialization.',
        long_help='Forward all arguments to the bundled mars CLI.',
    ),
    "skills": GroupHelp(
        summary="Read Meridian's bundled operational skills.",
        long_help=(
            "Read the on-demand guidance shipped with Meridian itself.\n\n"
            "Use `meridian skills list` to see names and triggers. "
            "`meridian skills NAME` prints the skill as a loaded skill: a header naming "
            "its folder, then its body. `meridian skills NAME RESOURCE-PATH` prints one "
            "file from that folder. Package-provided skills still load through the "
            "harness's native skill mechanism."
        ),
    ),
    "ext": GroupHelp(
        summary='Extension command discovery and invocation.',
        long_help='Extension command discovery and invocation.',
    ),
    "hooks": GroupHelp(
        summary='Hook inspection and execution commands.',
        long_help='Hook inspection and execution commands.',
    ),
    "task-dir": GroupHelp(
        summary='Query and mutate the current spawn source-edit directory.',
        long_help=(
            "Resolve or change where this session reads and edits source code.\n\n"
            "Distinct from `meridian work task-dir`, which sets durable work-item metadata."
        ),
    ),
    "sync": GroupHelp(
        summary='Sync operations and autosync conflict management.',
        long_help='Sync operations and autosync conflict management.',
    ),
    "models": GroupHelp(
        summary='Model catalog commands.',
        long_help='Model catalog commands.',
    ),
    "streaming": GroupHelp(
        summary='Streaming layer commands.',
        long_help='Streaming layer commands.',
    ),
    "test": GroupHelp(
        summary='Focused test and demo commands.',
        long_help='Focused test and demo commands.',
    ),
    "workspace": GroupHelp(
        summary='Workspace topology commands.',
        long_help='Workspace topology commands.\n\nShared conventions live in meridian.toml [workspace.NAME] entries; local overrides and additions live in meridian.local.toml.',
    ),
    "kg": GroupHelp(
        summary='Knowledge graph analysis: document relationships and link health.',
        long_help='Knowledge graph analysis: document relationships, tree topology, and broken link health.',
        examples=(('meridian kg graph', ''), ('meridian kg graph docs/', ''), ('meridian kg check', '')),
    ),
    "mermaid": GroupHelp(
        summary='Mermaid diagram validation.',
        long_help='Mermaid diagram validation: extract and check diagram syntax in markdown files and standalone .mmd/.mermaid files.',
        examples=(('meridian mermaid check', ''), ('meridian mermaid check docs/', ''), ('meridian mermaid check diagram.mmd', '')),
    ),
    "artifact": GroupHelp(
        summary='Share static artifacts over Tailscale.',
        long_help='Serve a local directory over Tailscale as a temporary, tailnet-scoped URL.',
        examples=(('meridian artifact serve .', ''), ('meridian artifact list', '')),
        agent_notes='URLs are tailnet-only — the recipient must be on your tailnet; use --funnel for a public URL (rare). Serves expire after 2h by default; --persistent keeps one, stop/gc remove them. Point it at a built structured-artifact directory to share with a human or another agent.',
        agent_subcommands=('serve', 'list', 'stop', 'gc'),
    ),
    "notify": GroupHelp(
        summary='Send a push and optional email notification.',
        long_help='Send one notification as the `notify.manual` hook event: a one-line title and an optional body. Its routes deliver it, by default once to the configured push and email backends, never retried in-process; `[[hooks]]` rows can reroute it or add command channels.',
        examples=(
            ("meridian notify \"Pick auth storage\" \"Tradeoffs in decisions.md\"", ''),
            ("meridian notify \"PR #412 ready for review\" --no-email", ''),
        ),
        agent_notes='The title is the one line the human reads first: what to decide or see. For when to notify, read `meridian skills notify__meridian`.',
    ),
    "idle": GroupHelp(
        summary="Manage harness idle schedules.",
        long_help="Adapter-facing idle scheduling and safety decisions.",
        agent_notes="Called by harness adapters; humans use `idle status`.",
        agent_subcommands=(
            "config",
            "arm",
            "return",
            "needs-permission",
            "fire",
            "done",
            "event",
            "status",
            "mod-path",
        ),
    ),
    "telemetry": GroupHelp(
        summary='Telemetry inspection: tail, query, and status over local segments.',
        long_help='Telemetry inspection: tail, query, and status over local segments.\n\nNote: Rootless MCP stdio server processes write telemetry to stderr only and are not visible in local segment readers.',
    ),
    "completion": GroupHelp(
        summary='Shell completion helpers.',
        long_help='Shell completion helpers.',
    ),
    "serve": GroupHelp(
        summary='Start FastMCP server on stdio.',
        long_help='Start FastMCP server on stdio.',
    ),
    "init": GroupHelp(
        summary='Initialize meridian in a project.',
        long_help='Initialize meridian in a project.',
    ),
    "bootstrap": GroupHelp(
        summary='Bootstrap an agent runtime.',
        long_help='Bootstrap an agent runtime.',
    ),
    "qi": GroupHelp(
        summary='Inline knowledge navigation: AGENTS.md and .context/ locations.',
        long_help='Inline knowledge navigation: AGENTS.md and .context/ locations.',
        agent_notes='Use `meridian qi graph <path>` before editing local knowledge files.',
        agent_subcommands=('graph', 'list', 'check'),
    ),
}

COMMAND_GROUP_SPECS: dict[str, CommandGroupSpec] = {
    "spawn": CommandGroupSpec(
        help=_GROUP_HELP["spawn"],
        agent_root_description='Hand a task to a subagent. Runs in the background; you keep working\nand collect the result later. (`wait`, `report`,\n`message` a running session)',
        agent_root=True,
        human_root_order=0,
        registration_bucket='spawn',
        has_group_app=True,
    ),
    "session": CommandGroupSpec(
        help=_GROUP_HELP["session"],
        agent_root_description='Read the full transcript of any spawn — what it did, what it found —\nso you build on it instead of redoing it.',
        agent_root=True,
        human_root_order=1,
        registration_bucket='session',
        has_group_app=True,
    ),
    "work": CommandGroupSpec(
        help=_GROUP_HELP["work"],
        agent_root_description='Tie related spawns to one goal with a shared folder and history, so\na multi-step effort holds together across handoffs.',
        agent_root=True,
        human_root_order=2,
        registration_bucket='work',
        has_group_app=True,
    ),
    "config": CommandGroupSpec(
        help=_GROUP_HELP["config"],
        human_root_order=8,
        registration_bucket='config',
        has_group_app=True,
    ),
    "context": CommandGroupSpec(
        help=_GROUP_HELP["context"],
        agent_root_description='Show the folders (as env vars) where shared files and project\nknowledge live, so you read and write them in the right place.',
        agent_root=True,
        registration_bucket='misc',
        has_group_app=True,
    ),
    "doctor": CommandGroupSpec(
        help=_GROUP_HELP["doctor"],
        human_root_order=18,
        registration_bucket='doctor',
    ),
    "mars": CommandGroupSpec(
        help=_GROUP_HELP["mars"],
        agent_root_description='List the models, agents, and skills available, so you pick real ones\nwhen you delegate. (`meridian mars models list`)',
        agent_root=True,
        human_root_order=15,
    ),
    "skills": CommandGroupSpec(
        help=_GROUP_HELP["skills"],
        agent_root_description=(
            "Read Meridian-specific judgment only when its trigger applies. "
            "(`meridian skills list`)"
        ),
        agent_root=True,
        human_root_order=16,
        registration_bucket="skills",
    ),
    "ext": CommandGroupSpec(
        help=_GROUP_HELP["ext"],
        agent_root_description='Run project-specific extension commands.',
        agent_root=True,
        human_root_order=20,
        registration_bucket='ext',
        has_group_app=True,
    ),
    "hooks": CommandGroupSpec(
        help=_GROUP_HELP["hooks"],
        human_root_order=3,
        registration_bucket='hooks',
        has_group_app=True,
    ),
    "task-dir": CommandGroupSpec(
        help=_GROUP_HELP["task-dir"],
        human_root_order=4,
        registration_bucket='task-dir',
        has_group_app=True,
    ),
    "sync": CommandGroupSpec(
        help=_GROUP_HELP["sync"],
        human_root_order=5,
        registration_bucket='sync',
    ),
    "models": CommandGroupSpec(
        help=_GROUP_HELP["models"],
        human_root_order=6,
        registration_bucket='models',
        has_group_app=True,
    ),
    "streaming": CommandGroupSpec(
        help=_GROUP_HELP["streaming"],
        human_root_order=7,
        registration_bucket='misc',
        has_group_app=True,
    ),
    "test": CommandGroupSpec(
        help=_GROUP_HELP["test"],
        human_root_order=8,
        registration_bucket='misc',
        has_group_app=True,
    ),
    "workspace": CommandGroupSpec(
        help=_GROUP_HELP["workspace"],
        human_root_order=9,
        registration_bucket='workspace',
        has_group_app=True,
    ),
    "kg": CommandGroupSpec(
        help=_GROUP_HELP["kg"],
        human_root_order=10,
        registration_bucket='kg',
        has_group_app=True,
    ),
    "mermaid": CommandGroupSpec(
        help=_GROUP_HELP["mermaid"],
        human_root_order=11,
        registration_bucket='mermaid',
        has_group_app=True,
    ),
    "artifact": CommandGroupSpec(
        help=_GROUP_HELP["artifact"],
        agent_root_description='Share a directory as a temporary Tailscale URL for humans or other agents.',
        agent_root=True,
        human_root_order=17,
        registration_bucket='artifact',
        has_group_app=True,
    ),
    "notify": CommandGroupSpec(
        help=_GROUP_HELP["notify"],
        agent_root_description='Send the human a notification.',
        agent_root=True,
        human_root_order=18,
        registration_bucket='notify',
        has_group_app=True,
    ),
    "idle": CommandGroupSpec(
        help=_GROUP_HELP["idle"],
        registration_bucket="idle",
        has_group_app=True,
        hidden_from_root_help=True,
    ),
    "telemetry": CommandGroupSpec(
        help=_GROUP_HELP["telemetry"],
        human_root_order=12,
        registration_bucket='telemetry',
        has_group_app=True,
    ),
    "completion": CommandGroupSpec(
        help=_GROUP_HELP["completion"],
        human_root_order=13,
        registration_bucket='misc',
        has_group_app=True,
    ),
    "serve": CommandGroupSpec(
        help=_GROUP_HELP["serve"],
        human_root_order=14,
    ),
    "init": CommandGroupSpec(
        help=_GROUP_HELP["init"],
        human_root_order=16,
    ),
    "bootstrap": CommandGroupSpec(
        help=_GROUP_HELP["bootstrap"],
        human_root_order=19,
        registration_bucket='bootstrap',
    ),
    "qi": CommandGroupSpec(
        help=_GROUP_HELP["qi"],
        registration_bucket='qi',
        has_group_app=True,
    ),
}

GROUPS: dict[str, GroupHelp] = {name: spec.help for name, spec in COMMAND_GROUP_SPECS.items()}

AGENT_ROOT_COMMANDS: tuple[str, ...] = tuple(
    name for name, spec in COMMAND_GROUP_SPECS.items() if spec.agent_root
)

HUMAN_ROOT_ORDER: tuple[str, ...] = tuple(
    name
    for name, _ in sorted(
        (
            (name, spec.human_root_order)
            for name, spec in COMMAND_GROUP_SPECS.items()
            if spec.human_root_order is not None
        ),
        key=lambda item: item[1],
    )
)

HIDDEN_ROOT_COMMANDS: frozenset[str] = frozenset(
    name for name, spec in COMMAND_GROUP_SPECS.items() if spec.hidden_from_root_help
)

AGENT_DESCRIPTION_OVERRIDES: dict[str, str] = {
    name: spec.agent_root_description
    for name, spec in COMMAND_GROUP_SPECS.items()
    if spec.agent_root_description is not None
}

GROUP_DESCRIPTIONS: dict[str, str] = {name: spec.help.summary for name, spec in COMMAND_GROUP_SPECS.items()}

COMMAND_REGISTRATION: dict[str, str] = {
    name: spec.registration_bucket
    for name, spec in COMMAND_GROUP_SPECS.items()
    if spec.registration_bucket is not None
}

AGENT_VISIBLE_SUBCOMMANDS: dict[str, tuple[str, ...]] = {
    name: spec.help.agent_subcommands
    for name, spec in COMMAND_GROUP_SPECS.items()
    if spec.help.agent_subcommands is not None
}

AGENT_CORE_SUBCOMMANDS: dict[str, tuple[str, ...]] = {
    name: spec.help.agent_core_subcommands
    for name, spec in COMMAND_GROUP_SPECS.items()
    if spec.help.agent_core_subcommands is not None
}

AGENT_HELP_SUPPLEMENTS: dict[str, str] = {
    name: spec.help.agent_notes
    for name, spec in COMMAND_GROUP_SPECS.items()
    if spec.help.agent_notes is not None
}


def register_group_app(name: str, app: App) -> None:
    """Record a Cyclopts group ``App`` for curated help lookup."""

    _group_apps[name] = app


def group_app(name: str) -> App | None:
    return _group_apps.get(name)


def group_apps_with_static_apps() -> dict[str, App]:
    """Return group apps registered at import time (no root app walk)."""

    return dict(_group_apps)


def supports_advanced_help(group_name: str) -> bool:
    spec = COMMAND_GROUP_SPECS.get(group_name)
    return spec is not None and spec.help.agent_core_subcommands is not None


def render_group_help(group_name: str, *, agent_mode: bool, advanced: bool = False) -> str:
    """Render the group description for human or agent help mode."""

    group = GROUPS[group_name]
    sections = [group.long_help or group.summary]
    lean = agent_mode and not advanced and group.agent_notes_brief is not None

    if group.examples and not lean:
        example_lines: list[str] = []
        for invocation, note in group.examples:
            if note:
                example_lines.append(f"  {invocation}  # {note}")
            else:
                example_lines.append(f"  {invocation}")
        sections.append("Usage examples:\n\n" + "\n\n".join(example_lines))

    if lean:
        agent_notes_brief = group.agent_notes_brief
        if agent_notes_brief is not None:
            sections.append("Playbook:\n\n" + agent_notes_brief.rstrip())
    elif agent_mode and group.agent_notes:
        sections.append("Agent Notes:\n\n" + group.agent_notes.rstrip())

    return "\n\n".join(sections)

__all__ = [
    "AGENT_CORE_SUBCOMMANDS",
    "AGENT_DESCRIPTION_OVERRIDES",
    "AGENT_HELP_SUPPLEMENTS",
    "AGENT_ROOT_COMMANDS",
    "AGENT_VISIBLE_SUBCOMMANDS",
    "COMMAND_GROUP_SPECS",
    "COMMAND_REGISTRATION",
    "GROUPS",
    "GROUP_DESCRIPTIONS",
    "HIDDEN_ROOT_COMMANDS",
    "HUMAN_ROOT_ORDER",
    "CommandGroupSpec",
    "GroupHelp",
    "group_app",
    "group_apps_with_static_apps",
    "register_group_app",
    "render_group_help",
    "supports_advanced_help",
]
