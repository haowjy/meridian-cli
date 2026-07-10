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
class RedirectPolicy:
    """Descriptor-owned redirect policy for commands handled elsewhere."""

    target: str
    message: str | None = None


@dataclass(frozen=True)
class CommandDescriptor:
    """Import-cheap metadata needed to classify a CLI command before dispatch."""

    command_path: tuple[str, ...]
    lazy_target: str
    summary: str
    startup_class: StartupClass
    bootstrap_plan: BootstrapPlan
    telemetry_mode: TelemetryMode
    default_output_mode: str
    redirect: RedirectPolicy | None = None
    extension_ref: str | None = None
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
    lazy_target: str | None = None,
    redirect: RedirectPolicy | None = None,
    extension_ref: str | None = None,
    root_source: RootSource = RootSource.CWD,
    auto_init_cwd: bool = False,
) -> CommandDescriptor:
    target = lazy_target or f"meridian.cli.main:{'.'.join(path) if path else 'root'}"
    bootstrap_plan = BootstrapPlan(
        StateRequirement.NONE if root_source == RootSource.ARGV else state_requirement,
        auto_init_cwd=auto_init_cwd,
    )
    return CommandDescriptor(
        command_path=path,
        lazy_target=target,
        summary=summary,
        startup_class=startup_class,
        bootstrap_plan=bootstrap_plan,
        telemetry_mode=telemetry_mode,
        default_output_mode=default_output_mode,
        redirect=redirect,
        extension_ref=extension_ref,
        root_source=root_source,
    )


def _read_project(
    path: tuple[str, ...], summary: str, *, extension_ref: str | None = None
) -> CommandDescriptor:
    return _descriptor(
        path,
        StartupClass.READ_PROJECT,
        StateRequirement.PROJECT_READ,
        TelemetryMode.NONE,
        "text",
        summary,
        extension_ref=extension_ref,
    )


def _read_rootless(
    path: tuple[str, ...],
    summary: str,
    *,
    startup_class: StartupClass = StartupClass.READ_ROOTLESS,
    default_output_mode: str = "text",
    extension_ref: str | None = None,
) -> CommandDescriptor:
    """Path- or user-level read commands that do not require a Meridian project."""

    return _descriptor(
        path,
        startup_class,
        StateRequirement.NONE,
        TelemetryMode.NONE,
        default_output_mode,
        summary,
        extension_ref=extension_ref,
    )


def _write_rootless(
    path: tuple[str, ...],
    summary: str,
    *,
    default_output_mode: str = "text",
    extension_ref: str | None = None,
) -> CommandDescriptor:
    """Path- or user-level write commands that do not require a Meridian project."""

    return _descriptor(
        path,
        StartupClass.WRITE_ROOTLESS,
        StateRequirement.NONE,
        TelemetryMode.NONE,
        default_output_mode,
        summary,
        extension_ref=extension_ref,
    )


def _write_project(
    path: tuple[str, ...],
    summary: str,
    *,
    extension_ref: str | None = None,
    root_source: RootSource = RootSource.CWD,
    auto_init_cwd: bool = False,
) -> CommandDescriptor:
    return _descriptor(
        path,
        StartupClass.WRITE_PROJECT,
        StateRequirement.PROJECT_WRITE,
        TelemetryMode.SEGMENT_OPTIONAL,
        "text",
        summary,
        extension_ref=extension_ref,
        root_source=root_source,
        auto_init_cwd=auto_init_cwd,
    )


def _read_runtime(
    path: tuple[str, ...],
    summary: str,
    *,
    default_output_mode: str = "text",
    extension_ref: str | None = None,
) -> CommandDescriptor:
    return _descriptor(
        path,
        StartupClass.READ_RUNTIME,
        StateRequirement.RUNTIME_READ,
        TelemetryMode.NONE,
        default_output_mode,
        summary,
        extension_ref=extension_ref,
    )


def _write_runtime(
    path: tuple[str, ...],
    summary: str,
    *,
    default_output_mode: str = "text",
    extension_ref: str | None = None,
    auto_init_cwd: bool = False,
) -> CommandDescriptor:
    return _descriptor(
        path,
        StartupClass.WRITE_RUNTIME,
        StateRequirement.RUNTIME_WRITE,
        TelemetryMode.SEGMENT,
        default_output_mode,
        summary,
        extension_ref=extension_ref,
        auto_init_cwd=auto_init_cwd,
    )


_COMMAND_DESCRIPTORS: tuple[CommandDescriptor, ...] = (
    _descriptor(
        (),
        StartupClass.PRIMARY_LAUNCH,
        StateRequirement.RUNTIME_WRITE,
        TelemetryMode.SEGMENT,
        "text",
        "Launch or resume the primary harness.",
        auto_init_cwd=True,
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
        "List model catalog inventory.",
        redirect=RedirectPolicy(
            target="meridian mars models list", message="Redirecting to meridian mars models list"
        ),
        extension_ref="meridian.models.list",
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
        extension_ref="meridian.spawn.create",
        auto_init_cwd=True,
    ),
    _write_runtime(
        ("spawn", "create"),
        "Create a spawn.",
        default_output_mode="text",
        extension_ref="meridian.spawn.create",
        auto_init_cwd=True,
    ),
    _write_runtime(
        ("spawn", "continue"),
        "Continue a spawn.",
        default_output_mode="text",
        extension_ref="meridian.spawn.continue",
    ),
    _read_runtime(
        ("spawn", "list"),
        "List spawns.",
        default_output_mode="text",
        extension_ref="meridian.spawn.list",
    ),
    _read_runtime(
        ("spawn", "show"),
        "Show spawn details.",
        default_output_mode="text",
        extension_ref="meridian.spawn.show",
    ),
    _read_runtime(
        ("spawn", "status"),
        "Show spawn status details without report body by default.",
        default_output_mode="text",
        extension_ref="meridian.spawn.status",
    ),
    _read_runtime(
        ("spawn", "wait"),
        "Wait for spawns.",
        default_output_mode="text",
        extension_ref="meridian.spawn.wait",
    ),
    _write_runtime(
        ("spawn", "cancel"),
        "Cancel a spawn.",
        default_output_mode="text",
        extension_ref="meridian.spawn.cancel",
    ),
    _write_runtime(
        ("spawn", "cancel-all"),
        "Cancel all spawns.",
        default_output_mode="text",
        extension_ref="meridian.spawn.cancelAll",
    ),
    _write_runtime(
        ("spawn", "inject"), "Inject a message into a spawn.", default_output_mode="text"
    ),
    _read_runtime(
        ("spawn", "children"),
        "List child spawns.",
        default_output_mode="text",
        extension_ref="meridian.spawn.children",
    ),
    _read_runtime(
        ("spawn", "subagents"),
        "List subagents the current agent may spawn.",
        extension_ref="meridian.spawn.subagents",
    ),
    _read_runtime(
        ("spawn", "files"), "List files changed by a spawn.", extension_ref="meridian.spawn.files"
    ),
    _read_runtime(("spawn", "stats"), "Show spawn stats.", extension_ref="meridian.spawn.stats"),
    _read_runtime(
        ("spawn", "report", "show"), "Show a spawn report.", extension_ref="meridian.report.show"
    ),
    _read_runtime(
        ("spawn", "report", "search"),
        "Search spawn reports.",
        extension_ref="meridian.report.search",
    ),
    _read_runtime(("session", "log"), "Show session log.", extension_ref="meridian.session.log"),
    _read_runtime(
        ("session", "export"), "Export session log.", extension_ref="meridian.session.export"
    ),
    _read_runtime(
        ("session", "search"), "Search session logs.", extension_ref="meridian.session.search"
    ),
    _write_runtime(
        ("session", "repair"),
        "Repair stored session metadata from explicit harness-session detection.",
        extension_ref="meridian.session.repair",
    ),
    _read_runtime(("work",), "Show work dashboard."),
    _read_runtime(("work", "list"), "List work items.", extension_ref="meridian.work.list"),
    _read_runtime(("work", "show"), "Show work item.", extension_ref="meridian.work.show"),
    _read_runtime(
        ("work", "sessions"), "Show sessions for work.", extension_ref="meridian.work.sessions"
    ),
    _read_runtime(("work", "current"), "Show current work.", extension_ref="meridian.work.current"),
    _write_runtime(
        ("work", "path"),
        "Materialize a path under the active work scope.",
        extension_ref="meridian.work.path",
    ),
    _read_runtime(("work", "root"), "Show work root.", extension_ref="meridian.work.root"),
    _write_runtime(
        ("work", "start"),
        "Start work item.",
        extension_ref="meridian.work.start",
        auto_init_cwd=True,
    ),
    _write_runtime(
        ("work", "switch"), "Switch current work.", extension_ref="meridian.work.switch"
    ),
    _write_runtime(("work", "done"), "Mark work done.", extension_ref="meridian.work.done"),
    _write_runtime(("work", "reopen"), "Reopen work item.", extension_ref="meridian.work.reopen"),
    _write_runtime(("work", "update"), "Update work item.", extension_ref="meridian.work.update"),
    _write_runtime(("work", "delete"), "Delete work item.", extension_ref="meridian.work.delete"),
    _write_runtime(("work", "rename"), "Rename work item.", extension_ref="meridian.work.rename"),
    _write_runtime(("work", "clear"), "Clear current work.", extension_ref="meridian.work.clear"),
    _write_runtime(
        ("work", "task-dir"),
        "Print or set the active work item's source-code edit directory.",
        extension_ref="meridian.work.task-dir",
    ),
    _read_rootless(
        ("config", "show"), "Show resolved config.", extension_ref="meridian.config.show"
    ),
    _read_rootless(("config", "get"), "Get config value.", extension_ref="meridian.config.get"),
    _write_project(
        ("config", "init"),
        "Initialize config.",
        extension_ref="meridian.config.init",
        root_source=RootSource.ARGV,
    ),
    _write_project(
        ("config", "set"),
        "Set config value.",
        extension_ref="meridian.config.set",
        auto_init_cwd=True,
    ),
    _write_project(
        ("config", "reset"), "Reset config value.", extension_ref="meridian.config.reset"
    ),
    _read_project(("context",), "Show context paths.", extension_ref="meridian.context.context"),
    _read_rootless(
        ("doctor",),
        "Run doctor checks.",
        extension_ref="meridian.doctor.doctor",
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
    _read_project(("hooks", "list"), "List hooks.", extension_ref="meridian.hooks.list"),
    _read_project(
        ("hooks", "check"), "Check hook configuration.", extension_ref="meridian.hooks.check"
    ),
    _write_project(("hooks", "run"), "Run hooks.", extension_ref="meridian.hooks.run"),
    _read_runtime(
        ("task-dir",),
        "Print effective source-edit directory.",
        extension_ref="meridian.task-dir",
    ),
    _write_runtime(
        ("task-dir", "set"),
        "Set spawn-scope source-edit directory.",
        extension_ref="meridian.task-dir.set",
    ),
    _write_runtime(
        ("task-dir", "clear"),
        "Clear spawn-scope source-edit directory override.",
        extension_ref="meridian.task-dir.clear",
    ),
    _read_runtime(("sync", "conflict", "list"), "List unresolved autosync conflicts."),
    _read_runtime(("sync", "conflict", "show"), "Show autosync conflict details."),
    _write_runtime(("sync", "conflict", "resolve"), "Mark autosync conflict resolved."),
    _read_project(("workspace", "list"), "List workspaces."),
    _write_project(
        ("workspace", "init"),
        "Initialize workspace config.",
        extension_ref="meridian.workspace.init",
        auto_init_cwd=True,
    ),
    _read_rootless(("kg", "graph"), "Render knowledge graph."),
    _read_rootless(("kg", "check"), "Check knowledge graph links."),
    _read_rootless(("mermaid", "check"), "Check Mermaid diagrams."),
    _write_rootless(("artifact", "serve"), "Serve a directory over tailscale."),
    _read_rootless(("artifact", "list"), "List active artifact serves."),
    _write_rootless(("artifact", "stop"), "Stop an artifact serve."),
    _write_rootless(("artifact", "gc"), "Garbage-collect expired artifact serves."),
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
        auto_init_cwd=True,
    ),
)

COMMAND_CATALOG = CommandCatalog(_COMMAND_DESCRIPTORS)

__all__ = [
    "COMMAND_CATALOG",
    "CommandCatalog",
    "CommandDescriptor",
    "RedirectPolicy",
]
