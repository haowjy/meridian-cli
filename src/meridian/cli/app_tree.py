"""CLI app tree objects shared by the main router."""

from cyclopts import App

from meridian import __version__
from meridian.cli.help_content import GROUPS


def _group_help(name: str) -> str:
    group = GROUPS[name]
    return group.long_help or group.summary


def _group_app(name: str) -> App:
    return App(
        name=name,
        help=_group_help(name),
        help_epilogue="",
        help_formatter="plain",
        help_format="plaintext",
    )


app = App(
    name="meridian",
    help="Multi-agent orchestration across Claude, Codex, and OpenCode.",
    version=__version__,
    help_formatter="plain",
    help_format="plaintext",
)
spawn_app = _group_app("spawn")
report_app = App(
    name="report",
    help="Report management commands.",
    help_epilogue="",
    help_formatter="plain",
    help_format="plaintext",
)
session_app = _group_app("session")
work_app = _group_app("work")
hooks_app = _group_app("hooks")
models_app = _group_app("models")
streaming_app = _group_app("streaming")
test_app = _group_app("test")
config_app = _group_app("config")
workspace_app = _group_app("workspace")
kg_app = _group_app("kg")
mermaid_app = _group_app("mermaid")
qi_app = _group_app("qi")
telemetry_app = _group_app("telemetry")
completion_app = _group_app("completion")
ext_app = _group_app("ext")

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
