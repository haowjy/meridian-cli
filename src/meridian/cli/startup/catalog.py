"""Startup-cheap command descriptors and the current CLI command catalog."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from meridian.cli.startup.policy import (
    BootstrapPlan,
    RootSource,
    StartupClass,
    StateRequirement,
    TelemetryMode,
)


@dataclass(frozen=True)
class CommandDescriptor:
    """Import-cheap metadata needed to classify a CLI command before dispatch."""

    command_path: tuple[str, ...]
    summary: str
    startup_class: StartupClass
    bootstrap_plan: BootstrapPlan
    telemetry_mode: TelemetryMode
    default_output_mode: str
    root_source: RootSource = RootSource.CWD


class CommandCatalog:
    """In-memory catalog with longest-prefix command classification."""

    def __init__(self, descriptors: Sequence[CommandDescriptor]) -> None:
        self._descriptors = tuple(descriptors)
        self._by_path = {descriptor.command_path: descriptor for descriptor in descriptors}

    def classify(self, argv: Sequence[str]) -> CommandDescriptor | None:
        """Return the longest descriptor whose command path prefixes ``argv``."""

        tokens = tuple(argv)
        if not tokens:
            return self._by_path.get(())

        for length in range(len(tokens), 0, -1):
            descriptor = self._by_path.get(tokens[:length])
            if descriptor is not None:
                return descriptor
        return None

    def get(self, command_path: tuple[str, ...]) -> CommandDescriptor | None:
        """Return descriptor for an exact command path."""

        return self._by_path.get(command_path)

    def all_descriptors(self) -> Sequence[CommandDescriptor]:
        """Return all descriptors in declaration order."""

        return self._descriptors

    def top_level_names(self) -> set[str]:
        """Return unique first command tokens in this catalog."""

        return {
            descriptor.command_path[0]
            for descriptor in self._descriptors
            if descriptor.command_path
        }


def _descriptor(
    path: tuple[str, ...],
    startup_class: StartupClass,
    state_requirement: StateRequirement,
    telemetry_mode: TelemetryMode,
    default_output_mode: str,
    summary: str,
    *,
    root_source: RootSource = RootSource.CWD,
) -> CommandDescriptor:
    bootstrap_plan = BootstrapPlan(
        StateRequirement.NONE if root_source == RootSource.ARGV else state_requirement
    )
    return CommandDescriptor(
        command_path=path,
        summary=summary,
        startup_class=startup_class,
        bootstrap_plan=bootstrap_plan,
        telemetry_mode=telemetry_mode,
        default_output_mode=default_output_mode,
        root_source=root_source,
    )


def _read_project(path: tuple[str, ...], summary: str) -> CommandDescriptor:
    return _descriptor(
        path,
        StartupClass.READ_PROJECT,
        StateRequirement.PROJECT_READ,
        TelemetryMode.NONE,
        "text",
        summary,
    )


def _read_rootless(
    path: tuple[str, ...],
    summary: str,
    *,
    startup_class: StartupClass = StartupClass.READ_ROOTLESS,
    default_output_mode: str = "text",
) -> CommandDescriptor:
    """Path- or user-level read commands that do not require a Meridian project."""

    return _descriptor(
        path,
        startup_class,
        StateRequirement.NONE,
        TelemetryMode.NONE,
        default_output_mode,
        summary,
    )


def _write_rootless(
    path: tuple[str, ...],
    summary: str,
    *,
    default_output_mode: str = "text",
) -> CommandDescriptor:
    """Path- or user-level write commands that do not require a Meridian project."""

    return _descriptor(
        path,
        StartupClass.WRITE_ROOTLESS,
        StateRequirement.NONE,
        TelemetryMode.NONE,
        default_output_mode,
        summary,
    )


def _write_project(
    path: tuple[str, ...],
    summary: str,
    *,
    root_source: RootSource = RootSource.CWD,
) -> CommandDescriptor:
    return _descriptor(
        path,
        StartupClass.WRITE_PROJECT,
        StateRequirement.PROJECT_WRITE,
        TelemetryMode.SEGMENT_OPTIONAL,
        "text",
        summary,
        root_source=root_source,
    )


def _read_runtime(
    path: tuple[str, ...],
    summary: str,
    *,
    default_output_mode: str = "text",
) -> CommandDescriptor:
    return _descriptor(
        path,
        StartupClass.READ_RUNTIME,
        StateRequirement.RUNTIME_READ,
        TelemetryMode.NONE,
        default_output_mode,
        summary,
    )


def _write_runtime(
    path: tuple[str, ...],
    summary: str,
    *,
    default_output_mode: str = "text",
) -> CommandDescriptor:
    return _descriptor(
        path,
        StartupClass.WRITE_RUNTIME,
        StateRequirement.RUNTIME_WRITE,
        TelemetryMode.SEGMENT,
        default_output_mode,
        summary,
    )


_COMMAND_DESCRIPTORS: tuple[CommandDescriptor, ...] = (
    _descriptor(
        (),
        StartupClass.PRIMARY_LAUNCH,
        StateRequirement.RUNTIME_WRITE,
        TelemetryMode.SEGMENT,
        "text",
        "Launch or resume the primary harness.",
    ),
    _descriptor(
        ("serve",),
        StartupClass.SERVICE_ROOTLESS,
        StateRequirement.NONE,
        TelemetryMode.STDERR,
        "text",
        "Start FastMCP server on stdio.",
    ),
    _descriptor(
        ("mars",),
        StartupClass.TRIVIAL,
        StateRequirement.NONE,
        TelemetryMode.NONE,
        "text",
        "Forward arguments to the bundled mars CLI.",
    ),
    _descriptor(
        ("models", "list"),
        StartupClass.TRIVIAL,
        StateRequirement.NONE,
        TelemetryMode.NONE,
        "text",
        "Inspect the curated harness-model view.",
    ),
    _descriptor(
        ("completion", "bash"),
        StartupClass.TRIVIAL,
        StateRequirement.NONE,
        TelemetryMode.NONE,
        "text",
        "Emit bash completion script.",
    ),
    _descriptor(
        ("completion", "zsh"),
        StartupClass.TRIVIAL,
        StateRequirement.NONE,
        TelemetryMode.NONE,
        "text",
        "Emit zsh completion script.",
    ),
    _descriptor(
        ("completion", "fish"),
        StartupClass.TRIVIAL,
        StateRequirement.NONE,
        TelemetryMode.NONE,
        "text",
        "Emit fish completion script.",
    ),
    _descriptor(
        ("completion", "install"),
        StartupClass.TRIVIAL,
        StateRequirement.NONE,
        TelemetryMode.NONE,
        "text",
        "Install shell completion.",
    ),
    _write_runtime(
        ("spawn",),
        "Create a spawn via spawn default route.",
        default_output_mode="text",
    ),
    _read_runtime(
        ("spawn", "list"),
        "List spawns.",
        default_output_mode="text",
    ),
    _read_runtime(
        ("spawn", "show"),
        "Show spawn details.",
        default_output_mode="text",
    ),
    _read_runtime(
        ("spawn", "status"),
        "Show spawn status details without report body by default.",
        default_output_mode="text",
    ),
    _read_runtime(
        ("spawn", "wait"),
        "Wait for spawns.",
        default_output_mode="text",
    ),
    _write_runtime(
        ("spawn", "cancel"),
        "Cancel a spawn.",
        default_output_mode="text",
    ),
    _write_runtime(
        ("spawn", "cancel-all"),
        "Cancel all spawns.",
        default_output_mode="text",
    ),
    _write_runtime(("spawn", "done"), "Signal a resident spawn to finish successfully."),
    _write_runtime(("spawn", "rearm"), "Signal a resident spawn to keep running."),
    _write_runtime(
        ("spawn", "inject"), "Inject a message into a spawn.", default_output_mode="text"
    ),
    _read_runtime(
        ("spawn", "children"),
        "List child spawns.",
        default_output_mode="text",
    ),
    _read_runtime(
        ("spawn", "subagents"),
        "List subagents the current agent may spawn.",
    ),
    _read_runtime(("spawn", "files"), "List files changed by a spawn."),
    _read_runtime(("spawn", "stats"), "Show spawn stats."),
    _read_runtime(("spawn", "report", "show"), "Show a spawn report."),
    _read_runtime(
        ("spawn", "report", "search"),
        "Search spawn reports.",
    ),
    _read_runtime(
        ("session", "browse"),
        "Browse recent sessions; resume stopped or fork live ones.",
    ),
    _read_runtime(("session", "log"), "Show session log."),
    _read_runtime(("session", "export"), "Export session log."),
    _read_runtime(("session", "search"), "Search session logs."),
    _write_runtime(
        ("session", "repair"),
        "Repair stored session metadata from explicit harness-session detection.",
    ),
    _write_runtime(
        ("session", "archive"),
        "Archive retained history.",
    ),
    _write_runtime(
        ("session", "import"), "Import history ZIP."
    ),
    _write_runtime(
        ("session", "restore"), "Restore inert history."
    ),
    _write_runtime(
        ("session", "index"),
        "Inspect or rebuild history index.",
    ),
    _read_runtime(("work",), "Show work dashboard."),
    _read_runtime(("work", "list"), "List work items."),
    _read_runtime(("work", "show"), "Show work item."),
    _read_runtime(
        ("work", "sessions"), "Show sessions for work."
    ),
    _read_runtime(("work", "current"), "Show current work."),
    _write_runtime(
        ("work", "path"),
        "Materialize a path under the active work scope.",
    ),
    _read_runtime(("work", "root"), "Show work root."),
    _write_runtime(
        ("work", "start"),
        "Start work item.",
    ),
    _write_runtime(
        ("work", "switch"), "Switch current work."
    ),
    _write_runtime(("work", "done"), "Mark work done."),
    _write_runtime(("work", "reopen"), "Reopen work item."),
    _write_runtime(("work", "update"), "Update work item."),
    _write_runtime(("work", "delete"), "Delete work item."),
    _write_runtime(("work", "rename"), "Rename work item."),
    _write_runtime(("work", "clear"), "Clear current work."),
    _write_runtime(
        ("work", "task-dir"),
        "Print or set the active work item's source-code edit directory.",
    ),
    _read_rootless(
        ("config", "show"), "Show resolved config."
    ),
    _read_rootless(("config", "get"), "Get config value."),
    _write_project(
        ("config", "init"),
        "Initialize config.",
        root_source=RootSource.ARGV,
    ),
    _write_project(
        ("config", "set"),
        "Set config value.",
    ),
    _write_project(
        ("config", "reset"), "Reset config value."
    ),
    _read_project(("context",), "Show context paths."),
    _read_rootless(
        ("doctor",),
        "Run doctor checks.",
    ),
    _read_runtime(("telemetry", "status"), "Show telemetry status."),
    _read_runtime(("telemetry", "tail"), "Tail telemetry events."),
    _read_runtime(("telemetry", "query"), "Query telemetry events."),
    _write_project(("init",), "Initialize meridian in a project.", root_source=RootSource.ARGV),
    _write_project(("migrate",), "Migrate project ID from UUID to three-word format."),
    _read_runtime(("ext",), "List extension commands."),
    _read_rootless(
        ("ext", "list"),
        "List extensions.",
    ),
    _read_runtime(("ext", "show"), "Show extension."),
    _read_rootless(
        ("ext", "commands"),
        "List extension commands.",
    ),
    _read_runtime(("ext", "run"), "Run extension command."),
    _read_project(("hooks", "list"), "List hooks."),
    _read_project(
        ("hooks", "check"), "Check hook configuration."
    ),
    _write_project(("hooks", "run"), "Run hooks."),
    _read_runtime(
        ("task-dir",),
        "Print effective source-edit directory.",
    ),
    _write_runtime(
        ("task-dir", "set"),
        "Set spawn-scope source-edit directory.",
    ),
    _write_runtime(
        ("task-dir", "clear"),
        "Clear spawn-scope source-edit directory override.",
    ),
    _read_runtime(("sync", "conflict", "list"), "List unresolved autosync conflicts."),
    _read_runtime(("sync", "conflict", "show"), "Show autosync conflict details."),
    _write_runtime(("sync", "conflict", "resolve"), "Mark autosync conflict resolved."),
    _read_project(("workspace", "list"), "List workspaces."),
    _write_project(
        ("workspace", "init"),
        "Initialize workspace config.",
    ),
    _read_rootless(("kg", "graph"), "Render knowledge graph."),
    _read_rootless(("kg", "check"), "Check knowledge graph links."),
    _read_rootless(("mermaid", "check"), "Check Mermaid diagrams."),
    _write_rootless(("artifact", "serve"), "Serve a directory over tailscale."),
    _read_rootless(("artifact", "list"), "List active artifact serves."),
    _write_rootless(("artifact", "stop"), "Stop an artifact serve."),
    _write_rootless(("artifact", "gc"), "Garbage-collect expired artifact serves."),
    _write_rootless(("notify",), "Send a push and optional email notification."),
    _read_rootless(("idle",), "Manage harness idle schedules.", default_output_mode="json"),
    _read_rootless(("idle", "config"), "Resolve idle policy.", default_output_mode="json"),
    _write_rootless(("idle", "arm"), "Arm an idle schedule.", default_output_mode="json"),
    _write_rootless(("idle", "return"), "Close an idle stretch.", default_output_mode="json"),
    _write_rootless(("idle", "fire"), "Claim an idle stage.", default_output_mode="json"),
    _write_rootless(("idle", "done"), "Record an idle action result.", default_output_mode="json"),
    _write_rootless(("idle", "event"), "Handle a harness idle event.", default_output_mode="json"),
    _read_rootless(("idle", "status"), "Show open idle stretches."),
    _read_rootless(("idle", "mod-path"), "Print the bundled Claude idle mod path."),
    _read_rootless(("qi",), "Show inline knowledge for current path."),
    _read_rootless(("qi", "list"), "List all inline knowledge locations."),
    _read_rootless(("qi", "check"), "Check inline knowledge health."),
    _write_rootless(("qi", "claude-md-fix"), "Create CLAUDE.md mirrors for AGENTS.md files."),
    _write_runtime(("streaming", "test"), "Run streaming test."),
    _descriptor(
        ("streaming", "serve"),
        StartupClass.SERVICE_RUNTIME,
        StateRequirement.RUNTIME_WRITE,
        TelemetryMode.SEGMENT,
        "text",
        "Run streaming service.",
    ),
    _write_runtime(("test", "harness"), "Run harness test."),
    _descriptor(
        ("bootstrap",),
        StartupClass.WRITE_RUNTIME,
        StateRequirement.RUNTIME_WRITE,
        TelemetryMode.SEGMENT,
        "text",
        "Bootstrap an agent runtime.",
    ),
)

COMMAND_CATALOG = CommandCatalog(_COMMAND_DESCRIPTORS)

__all__ = [
    "COMMAND_CATALOG",
    "CommandCatalog",
    "CommandDescriptor",
]
