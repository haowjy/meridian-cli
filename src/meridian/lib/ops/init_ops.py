"""Init-with-add orchestration for meridian project setup."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.util import FormatContext


@dataclass(frozen=True)
class InitAddResult:
    """Parsed result from mars add --json."""

    declared_targets: list[str]
    declared_primary_agent: str | None
    add_report: dict[str, Any]


@dataclass(frozen=True)
class PrimaryAgentAction:
    action: str  # "none", "set", "already_set", "differs"
    agent: str | None = None
    current: str | None = None
    message: str | None = None


class InitResult(BaseModel):
    """Output from run_init_flow. Supports text and JSON output."""

    model_config = ConfigDict(frozen=True)

    ok: bool = True
    project_root: str
    config_created: bool
    packages_added: list[str]
    packages_resolved: list[str]
    targets_linked: list[str]
    content_count: int
    primary_agent: PrimaryAgentAction | None = None
    next_step: str = "Run `meridian` to start."

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        lines = [f"Initialized {self.project_root}", ""]
        if self.packages_resolved:
            pkg_str = ", ".join(self.packages_resolved)
            lines.append(f"  Packages:   {pkg_str} ({len(self.packages_resolved)} packages)")
        if self.targets_linked:
            lines.append(f"  Targets:    {', '.join(self.targets_linked)}")
        lines.append(f"  Content:    {self.content_count} items")
        if self.primary_agent and self.primary_agent.action == "set":
            lines.append(f"  Primary:    {self.primary_agent.agent} (set in meridian.toml)")
        elif self.primary_agent and self.primary_agent.action == "differs":
            lines.append(f"  Primary:    {self.primary_agent.message}")
        lines.append("")
        lines.append(f"{self.next_step}")
        return "\n".join(lines)


def run_mars_add_json(
    project_root: Path,
    sources: list[str],
    *,
    executable: str,
) -> InitAddResult:
    """Run mars add with JSON output and parse declared_targets."""

    from meridian.cli.mars_passthrough import (
        execute_mars_passthrough,
        parse_mars_passthrough,
    )

    args = ["--root", project_root.as_posix(), "--json", "add", *sources]
    request = parse_mars_passthrough(args, executable=executable, output_format="json")
    result = execute_mars_passthrough(request)
    if result.returncode != 0:
        if result.stderr_text:
            sys.stderr.write(result.stderr_text)
        raise SystemExit(result.returncode)
    parsed: dict[str, Any] = json.loads(result.stdout_text)
    return InitAddResult(
        declared_targets=parsed.get("declared_targets", []),
        declared_primary_agent=parsed.get("declared_primary_agent"),
        add_report=parsed,
    )


def run_mars_link_json(
    project_root: Path,
    target: str,
    *,
    executable: str,
) -> dict[str, Any]:
    """Run mars link for a single target with JSON output."""

    from meridian.cli.mars_passthrough import (
        execute_mars_passthrough,
        parse_mars_passthrough,
    )

    args = ["--root", project_root.as_posix(), "--json", "link", target]
    request = parse_mars_passthrough(args, executable=executable, output_format="json")
    result = execute_mars_passthrough(request)
    if result.returncode != 0:
        if result.stderr_text:
            sys.stderr.write(result.stderr_text)
        raise SystemExit(result.returncode)
    result_data: dict[str, Any] = json.loads(result.stdout_text) if result.stdout_text else {}
    return result_data


def run_mars_init_json(
    project_root: Path,
    *,
    executable: str,
) -> dict[str, Any]:
    """Run mars init with JSON output. Idempotent."""

    from meridian.cli.mars_passthrough import (
        execute_mars_passthrough,
        parse_mars_passthrough,
    )

    args = ["--root", project_root.as_posix(), "--json", "init"]
    request = parse_mars_passthrough(args, executable=executable, output_format="json")
    result = execute_mars_passthrough(request)
    if result.returncode != 0:
        if result.stderr_text:
            sys.stderr.write(result.stderr_text)
        raise SystemExit(result.returncode)
    result_data: dict[str, Any] = json.loads(result.stdout_text) if result.stdout_text else {}
    return result_data


def maybe_set_primary_agent(
    project_root: Path,
    declared_primary_agent: str | None,
) -> PrimaryAgentAction:
    """Set primary.agent if unset. Returns what happened."""

    if not declared_primary_agent:
        return PrimaryAgentAction(action="none")

    config_path = project_root / "meridian.toml"
    if not config_path.is_file():
        return PrimaryAgentAction(action="none")

    content = config_path.read_text(encoding="utf-8")

    import tomlkit

    doc = tomlkit.parse(content)
    primary_table = doc.get("primary")
    if isinstance(primary_table, dict):
        current_agent = primary_table.get("agent")
        if isinstance(current_agent, str) and current_agent.strip():
            if current_agent == declared_primary_agent:
                return PrimaryAgentAction(action="already_set", agent=declared_primary_agent)
            return PrimaryAgentAction(
                action="differs",
                agent=declared_primary_agent,
                current=current_agent,
                message=(
                    f"Package recommends '{declared_primary_agent}' as primary agent. "
                    f"Current primary is '{current_agent}'. "
                    f"Run `meridian -a {declared_primary_agent}` to try it, "
                    f"or update `meridian.toml` to change your default."
                ),
            )

    from meridian.lib.config.preserving_edit import set_scalar_option
    from meridian.lib.config.settings import OPTION_CATALOG
    from meridian.lib.state.atomic import atomic_write_text

    option = OPTION_CATALOG.resolve_key("primary.agent")
    edit_result = set_scalar_option(content, option=option, value=declared_primary_agent)
    atomic_write_text(config_path, edit_result.text)
    return PrimaryAgentAction(action="set", agent=declared_primary_agent)


def run_init_flow(
    *,
    project_root: Path,
    add_sources: list[str],
    link_targets: list[str] | None = None,
    yes: bool = False,
    output_format: str = "text",
) -> InitResult | dict[str, Any]:
    """Full init-with-add orchestration.

    Sequence:
    1. Bootstrap meridian.toml (config_init_sync)
    2. mars init if no mars.toml
    3. mars add <sources>
    4. Determine targets (--link overrides, else declared)
    5. mars link for each target
    6. Set primary agent if applicable
    7. Return result
    """

    from meridian.cli.mars_passthrough import resolve_mars_executable
    from meridian.lib.ops.config import ConfigInitInput, config_init_sync

    _ = yes  # reserved for future interactive prompts

    # 1. Bootstrap meridian.toml
    config_result = config_init_sync(ConfigInitInput(project_root=project_root.as_posix()))

    # Resolve mars executable
    executable = resolve_mars_executable()
    if executable is None:
        sys.stderr.write(
            "error: Failed to execute 'mars'. Install meridian with dependencies and retry.\n"
        )
        raise SystemExit(1)

    # 2. mars init if no mars.toml
    mars_toml = project_root / "mars.toml"
    if not mars_toml.is_file():
        run_mars_init_json(project_root, executable=executable)

    # 3. mars add
    add_result = run_mars_add_json(project_root, add_sources, executable=executable)

    # 4. Determine targets
    targets: list[str] = link_targets or add_result.declared_targets

    # 5. mars link for each target
    for target in targets:
        run_mars_link_json(project_root, target, executable=executable)

    # 6. Primary agent
    primary_action = maybe_set_primary_agent(project_root, add_result.declared_primary_agent)

    # 7. Build result
    report = add_result.add_report
    targets_data = report.get("targets", {})
    if isinstance(targets_data, dict) and targets_data:
        content_count = sum(
            int(t.get("synced", 0))
            for t in targets_data.values()
            if isinstance(t, dict)
        )
    else:
        content_count = int(report.get("installed", 0)) + int(report.get("updated", 0))

    result = InitResult(
        project_root=project_root.as_posix(),
        config_created=config_result.created,
        packages_added=add_sources,
        packages_resolved=add_sources,
        targets_linked=targets,
        content_count=content_count,
        primary_agent=primary_action,
    )

    if output_format == "json":
        return result.model_dump()
    return result
