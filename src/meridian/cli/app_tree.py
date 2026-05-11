"""CLI app tree objects shared by the main router."""

from cyclopts import App

from meridian import __version__
from meridian.cli.startup.help import AGENT_ROOT_HELP

app = App(
    name="meridian",
    help="Multi-agent orchestration across Claude, Codex, and OpenCode.",
    help_epilogue=(
        "Primary launch/resume:\n\n"
        "  meridian [-m MODEL]\n\n"
        "  meridian --continue c123\n\n"
        "  meridian --fork p123\n\n"
        "  refs: chat id (c123), spawn id (p123), or raw harness session id\n\n"
        "Global harness selection: --harness (or prefix with claude/codex/opencode)\n\n"
        "Bundled package manager: meridian mars ARGS...\n\n"
        'Run "meridian spawn -h" for subagent usage.\n'
    ),
    version=__version__,
    help_formatter="plain",
)
spawn_app = App(
    name="spawn",
    help=(
        "Run subagents with a model and prompt file.\n"
        "Runs in foreground by default; returns when the spawn reaches a terminal state. "
        "Foreground streaming uses terminal capture when available (Unix TTY sessions). "
        "On Windows or non-TTY shells, meridian falls back to subprocess capture. "
        "Use --background to return immediately with the spawn ID. "
        "For delegation, write the prompt to a file and pass --prompt-file."
    ),
    help_epilogue=(
        "Examples:\n\n"
        "  meridian spawn -m gpt-5.3-codex --prompt-file /tmp/fix-auth.md --bg\n\n"
        "  meridian spawn -m claude-sonnet-4-6 --prompt-file /tmp/review.md --bg -f src/main.py\n\n"
        "  meridian spawn --fork c123 --prompt-file /tmp/follow-up.md --bg\n\n"
        "  meridian spawn wait\n"
    ),
    help_formatter="plain",
)
report_app = App(
    name="report",
    help="Report management commands.",
    help_epilogue=(
        "Examples:\n\n"
        "  meridian spawn report show p107\n\n"
        '  meridian spawn report search "auth bug"\n'
    ),
    help_formatter="plain",
)
session_app = App(
    name="session",
    help=(
        "Inspect conversation and progress logs.\n\n"
        "Session refs accept three forms: chat ids (c123), spawn ids (p123),\n"
        "or raw harness session ids. Logs prefer harness transcripts when\n"
        "available and fall back to Meridian spawn output for active or\n"
        "transcriptless spawns. By default, commands operate on\n"
        "$MERIDIAN_CHAT_ID -- inherited from the spawning session -- so a\n"
        "subagent defaults to the top-level primary session log."
    ),
    help_formatter="plain",
)
work_app = App(
    name="work",
    help=(
        "Active activity grouped by work, plus work item coordination commands. "
        "Unassigned spawns appear under '(no work)'."
    ),
    help_formatter="plain",
)
hooks_app = App(name="hooks", help="Hook inspection and execution commands", help_formatter="plain")
models_app = App(name="models", help="Model catalog commands", help_formatter="plain")
streaming_app = App(name="streaming", help="Streaming layer commands", help_formatter="plain")
test_app = App(name="test", help="Focused test and demo commands", help_formatter="plain")
config_app = App(
    name="config",
    help=(
        "Repository-level config (meridian.toml) for default\n"
        "agent, model, harness, timeouts, and output verbosity.\n\n"
        "Resolved values are evaluated independently per field -- a CLI\n"
        "override on one field does not pull other fields from the same\n"
        "source. Use `meridian config show` to see each value with its\n"
        "source annotation."
    ),
    help_formatter="plain",
)
workspace_app = App(
    name="workspace",
    help=(
        "Workspace topology commands.\n\n"
        "Shared conventions live in meridian.toml [workspace.NAME] entries; "
        "local overrides and additions live in meridian.local.toml."
    ),
    help_formatter="plain",
)
kg_app = App(
    name="kg",
    help=(
        "Knowledge graph analysis: document relationships, tree topology,\nand broken link health."
    ),
    help_epilogue=(
        "Examples:\n\n  meridian kg graph\n\n  meridian kg graph docs/\n\n  meridian kg check\n"
    ),
    help_formatter="plain",
)
mermaid_app = App(
    name="mermaid",
    help=(
        "Mermaid diagram validation: extract and check diagram syntax in\n"
        "markdown files and standalone .mmd/.mermaid files."
    ),
    help_epilogue=(
        "Examples:\n\n"
        "  meridian mermaid check\n\n"
        "  meridian mermaid check docs/\n\n"
        "  meridian mermaid check diagram.mmd\n"
    ),
    help_formatter="plain",
)
qi_app = App(
    name="qi",
    help="Inline knowledge navigation: AGENTS.md and .context/ locations.",
    help_formatter="plain",
)
telemetry_app = App(
    name="telemetry",
    help="Telemetry inspection: tail, query, and status over local segments.",
    help_epilogue=(
        "Note: Rootless MCP stdio server processes write telemetry to stderr only "
        "and are not visible in local segment readers."
    ),
    help_formatter="plain",
)
completion_app = App(name="completion", help="Shell completion helpers", help_formatter="plain")
ext_app = App(
    name="ext",
    help="Extension command discovery and invocation.",
    help_formatter="plain",
)

app.command(spawn_app, name="spawn")
spawn_app.command(report_app, name="report")
app.command(session_app, name="session")
app.command(work_app, name="work")
app.command(hooks_app, name="hooks")
app.command(models_app, name="models")
app.command(streaming_app, name="streaming")
app.command(test_app, name="test")
app.command(config_app, name="config")
app.command(workspace_app, name="workspace")
app.command(kg_app, name="kg")
app.command(mermaid_app, name="mermaid")
app.command(qi_app, name="qi")
app.command(telemetry_app, name="telemetry")
app.command(completion_app, name="completion")

__all__ = [
    "AGENT_ROOT_HELP",
    "app",
    "completion_app",
    "config_app",
    "ext_app",
    "hooks_app",
    "kg_app",
    "mermaid_app",
    "models_app",
    "qi_app",
    "report_app",
    "session_app",
    "spawn_app",
    "streaming_app",
    "telemetry_app",
    "test_app",
    "work_app",
    "workspace_app",
]
