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


_group_apps: dict[str, App] = {}


_GROUP_HELP: dict[str, GroupHelp] = {
    "spawn": GroupHelp(
        summary='Run subagents with a model and prompt file.',
        long_help='Hand a task to a subagent — a bulk read, a parallel fix, or a job for a stronger or cheaper model — so you keep your own context for the parts that need your judgment. Write the task to a file and pass --prompt-file; add --bg to launch detached and collect the result with `meridian spawn wait`.',
        examples=(('meridian spawn -m gpt-5.3-codex --prompt-file "$(meridian work path prompts/fix-auth.md)" --bg', ''), ('meridian spawn -m claude-sonnet-4-6 --prompt-file "$(meridian work path prompts/review.md)" --bg -f src/main.py', ''), ('meridian spawn --fork c123 --prompt-file "$(meridian work path prompts/follow-up.md)" --bg', ''), ('meridian spawn --fork-fresh c123 -a reviewer --prompt-file "$(meridian work path prompts/role-shift.md)" --bg', ''), ('meridian spawn wait', '')),
        agent_notes="Core loop: launch detached with --bg, then drain with no-arg `meridian spawn wait`. Every --bg spawn must be drained before you respond to the user, start dependent work, or end your turn; un-waited background spawns are lost.\n\nLifecycle: queued → running → finalizing → terminal. Treat finalizing as active; terminal is succeeded, failed, cancelled, or timed_out.\n\nPrompt passing: use --prompt-file for delegation. Inline -p is for trivial smoke tests only because shell quoting mutates prompts ($vars, backticks, quotes, multiline) before meridian sees them. Run `meridian work path prompts/<name>.md`, write the prompt to the printed absolute path, then pass that path literally to --prompt-file. Disposable launch prompts belong under prompts/ (no /tmp, no shell var that won't survive the next Bash call).\n\nProfile vs model: prefer -a <profile> when a role fits; use -m <model> for one-offs. Put recurring model choices in profiles or config, not hardcoded spawn commands.\n\nContext refs: prefer a folder over a file bundle. A folder tells the agent where to work; it does not inline everything. Normal shape: one folder plus at most one source-of-truth file. AGENTS.md and .context/CONTEXT.md auto-load from the work directory, so don't pass them redundantly. Use --from for prior reasoning that is not in files.\n\nTask dir: source edits run in the work item's task_dir (check: `meridian task-dir`). --task-dir points one spawn at another checkout, e.g. a worktree lane; `meridian work task-dir <path>` rebinds the work item itself.\n\n--goal is a verifiable exit state, not a restatement of the task.\n\nParallel work: launch several --bg spawns, then one no-arg `meridian spawn wait` drains all. Don't babysit with repeated `spawn show` or `session log`; wait when you need results.\n\nReuse decision: --continue resumes the same session; --fork branches while preserving identity; --fork-fresh branches and allows identity changes, with weaker cache locality; --from starts fresh while carrying prior context as reference.\n\nInspect a run: `meridian session log <id>`; use `meridian spawn children <id>` to trace descendants.\nSteer a running spawn: `meridian spawn inject`.\nStage spawn changes: `meridian spawn files <id> | xargs git add`.",
        agent_notes_brief='Delegate with --prompt-file, never inline -p. The shell mangles a prompt before meridian sees it — backticks run as commands, $vars expand, quotes and newlines collapse. A file preserves exact text and keeps the handoff inspectable. Run `meridian work path prompts/<name>.md`, write there, then pass that path literally (no /tmp, and no $f shell var that won\'t survive your next Bash call):\n  meridian spawn -a coder --prompt-file "$(meridian work path prompts/task.md)" --bg\n\nPick the runner: -a <profile> when a role fits, -m <model> for a one-off. Keep recurring model choices in profiles or config, not in the command.\n\nPass context as a folder, not a file pile. A folder says where to work and lets the agent read what it needs; each extra -f may get inlined and drains its context. Usual shape: one folder plus at most one source-of-truth file. AGENTS.md and .context/CONTEXT.md auto-load from the work dir — don\'t re-pass them. Use --from for prior reasoning that isn\'t in any file.\n\nSource edits run in the work item\'s task_dir — check it with `meridian task-dir`. Pass --task-dir to point one spawn at another checkout (e.g. a worktree lane); rebind the work item itself with `meridian work task-dir <path>`.\n\nSet --goal to a verifiable exit state ("tests pass, lint clean"), not a restatement of the task — it\'s the contract the agent works until it meets.\n\nGoing parallel? Launch several --bg and let one `wait` drain them all; don\'t babysit with repeated `spawn show` / `session log` — that spends your context supervising instead of working.\n\nReuse a session with --continue. Full methodology and every flag — including --fork / --fork-fresh — is in `meridian spawn -h --advanced`.',
        agent_subcommands=('show', 'wait', 'list', 'children', 'files', 'report', 'inject', 'done', 'rearm', 'cancel', 'cancel-all'),
        agent_core_subcommands=('wait', 'show', 'list', 'inject', 'files', 'cancel'),
    ),
    "session": GroupHelp(
        summary='Browse sessions or inspect transcripts and progress logs.',
        long_help="Read back what a session did — its conversation and progress logs — to recover a\nsubagent's reasoning or find what was decided.\n\nRefs take three forms: chat ids (c123), spawn ids (p123), or raw harness session\nids. Commands default to $MERIDIAN_CHAT_ID (inherited from the spawning session),\nso omitting the ref inspects the top-level primary session at every depth; pass an\nexplicit spawn id to read one spawn's transcript.",
        agent_notes="Use `session search` to find transcript text; each hit prints a deterministic\nOpen command you can run directly. Cross compaction boundaries with `session log\n--segment previous` or `--global --around N`.\n\nRecover decisions across a work item's sessions with `meridian work sessions\nWORK_ID --all`.",
        agent_subcommands=('browse', 'log', 'search', 'export'),
    ),
    "work": GroupHelp(
        summary='Work item dashboard and coordination.',
        long_help="Group related runs under a work item — a shared id that keeps spawns, sessions,\nand scratch artifacts together so you can find them later. This view lists active\nruns grouped by work item; unassigned spawns appear under '(no work)'.",
        agent_notes='Artifact placement: $MERIDIAN_ACTIVE_WORK_DIR for this item,\n$MERIDIAN_CONTEXT_KB_DIR for project-wide durable knowledge.\n\nAttach delegated runs with `meridian spawn ... --work WORK_ID` so spawns, sessions, and scratch artifacts stay grouped.',
        agent_subcommands=('start', 'current', 'root', 'path', 'done', 'sessions', 'list', 'show', 'switch'),
        agent_core_subcommands=('start', 'current', 'root', 'path', 'done', 'sessions', 'list', 'show', 'switch'),
    ),
    "config": GroupHelp(
        summary='Show resolved configuration and sources.',
        long_help='Repository-level config (meridian.toml) for default\nagent, model, harness, timeouts, and output verbosity.\n\nResolved values are evaluated independently per field -- a CLI\noverride on one field does not pull other fields from the same\nsource. Use `meridian config show` to see each value with its\nsource annotation.',
        agent_notes='Resolution is per field:\nCLI flag > env var > profile > project > user > harness default\n\nA CLI model override (-m) also drives harness routing.',
        agent_subcommands=('show', 'get', 'set', 'init'),
    ),
    "context": GroupHelp(
        summary='Show context paths for work and knowledge.',
        long_help='Resolve context paths for handoffs and artifact placement — the active work dir\nand knowledge-base roots. With no args it prints the set; pass --name for one path.',
    ),
    "doctor": GroupHelp(
        summary='Health check and orphan reconciliation.',
        long_help="Health check and auto-repair for meridian state.\n\nReconciles orphaned spawns (dead PIDs, missing spawn directories),\ncleans stale session locks, scans telemetry retention, and warns about\nmissing or malformed configuration.\n\nUse --kill-orphans to terminate orphaned spawn process groups before\nfinalizing stale runs. Use --prune to delete stale spawn artifacts,\ntelemetry segments, and other stale retention targets for the current\nproject. Add --global to also prune stale orphan project dirs globally\nacross ~/.meridian/projects/.\n\nDoctor is idempotent - re-running converges on the same result. It is\nsafe (and intended) to run after a crash, after a force-kill, or any\ntime `meridian spawn show` reports a status that doesn't match reality.",
        examples=(('meridian doctor', 'check and repair'), ('meridian doctor --prune', 'prune stale artifacts + telemetry'), ('meridian doctor --prune --global', 'also prune other stale projects'), ('meridian doctor --kill-orphans', 'terminate orphaned process groups'), ('meridian doctor --format text', 'human-readable summary')),
        agent_notes="Run when a spawn seems stuck or status doesn't match reality.\nSpawn read paths (show, list, wait) and 'doctor' reconcile orphans.\n\nCommon failure modes:\n\n  orphan_run              Runner died mid-flight. Relaunch.\n\n  orphan_finalization     Exited without finalizing. Check 'spawn show'\n                          for partial report.\n\n  Exit 127 / empty report Harness binary missing from PATH.\n\n  Exit 143 or 137         Check 'spawn show' first — if already\n                          succeeded, signal hit during cleanup.\n                          Otherwise retry.\n\nFor the transcript: 'meridian session log SPAWN_ID'.",
    ),
    "mars": GroupHelp(
        summary='Package management and agent materialization.',
        long_help='Forward all arguments to the bundled mars CLI.',
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
        long_help='Knowledge graph analysis: document relationships, tree topology,\nand broken link health.',
        examples=(('meridian kg graph', ''), ('meridian kg graph docs/', ''), ('meridian kg check', '')),
    ),
    "mermaid": GroupHelp(
        summary='Mermaid diagram validation.',
        long_help='Mermaid diagram validation: extract and check diagram syntax in\nmarkdown files and standalone .mmd/.mermaid files.',
        examples=(('meridian mermaid check', ''), ('meridian mermaid check docs/', ''), ('meridian mermaid check diagram.mmd', '')),
    ),
    "artifact": GroupHelp(
        summary='Share static artifacts over Tailscale.',
        long_help='Serve a local directory over Tailscale as a temporary, tailnet-scoped URL.',
        examples=(('meridian artifact serve .', ''), ('meridian artifact list', '')),
        agent_notes='URLs are tailnet-only — the recipient must be on your tailnet; use --funnel for a public URL (rare). Serves expire after 2h by default; --persistent keeps one, stop/gc remove them. Point it at a built structured-artifact directory to share with a human or another agent.',
        agent_subcommands=('serve', 'list', 'stop', 'gc'),
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
    "migrate": GroupHelp(
        summary='Run state and configuration migrations.',
        long_help='Run state and configuration migrations.',
    ),
    "qi": GroupHelp(
        summary='Inline knowledge navigation: AGENTS.md and .context/ locations.',
        long_help='Inline knowledge navigation: AGENTS.md and .context/ locations.',
        agent_notes='Use `meridian qi graph <path>` before editing local knowledge files. Run `meridian qi claude-md-fix <path>` after adding or moving AGENTS.md files so Claude sees the same instructions via CLAUDE.md mirrors.',
        agent_subcommands=('graph', 'list', 'check', 'claude-md-fix'),
    ),
}

COMMAND_GROUP_SPECS: dict[str, CommandGroupSpec] = {
    "spawn": CommandGroupSpec(
        help=_GROUP_HELP["spawn"],
        agent_root_description='Hand a task to a subagent. Runs in the background; you keep working\nand collect the result later. (`wait`, `report`)',
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
    "migrate": CommandGroupSpec(
        help=_GROUP_HELP["migrate"],
        registration_bucket='migrate',
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


def registration_buckets() -> dict[str, frozenset[str]]:
    """Map lazy-registration bucket names to command group names."""

    buckets: dict[str, set[str]] = {}
    for name, spec in COMMAND_GROUP_SPECS.items():
        if spec.registration_bucket is None:
            continue
        buckets.setdefault(spec.registration_bucket, set()).add(name)
    return {bucket: frozenset(names) for bucket, names in buckets.items()}


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
    "HUMAN_ROOT_ORDER",
    "CommandGroupSpec",
    "GroupHelp",
    "group_app",
    "group_apps_with_static_apps",
    "register_group_app",
    "registration_buckets",
    "render_group_help",
    "supports_advanced_help",
]
